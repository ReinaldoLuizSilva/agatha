"""Configuração inicial do Zabbix de laboratório pela API (ADR-011). Pode ser rodado de novo.

1. Troca a senha padrão do Admin por uma aleatória (guardada em <lab>/admin-password, modo 600).
2. Cria o grupo "Agatha lab" e o host agatha-srv com o template "Linux by Zabbix agent"; o agente
   é alcançado por host.containers.internal (rede pasta do Podman sem root).
3. Desativa o host padrão "Zabbix server" (sem agente no pod, só geraria alerta de indisponível).
4. Cria o grupo "Agatha somente leitura" (leitura no "Agatha lab"), o usuário agatha e um token de
   API, e grava AGATHA_ZABBIX_URL e AGATHA_ZABBIX_TOKEN no server/.env.

Uso: .venv/bin/python deploy/zabbix/setup.py
"""

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("ZABBIX_API", "http://127.0.0.1:8081/api_jsonrpc.php")
LAB = Path(os.environ.get("ZABBIX_LAB_DIR", "/dados/agatha/lab/zabbix"))
ENV_FILE = Path(__file__).resolve().parents[2] / "server" / ".env"
AGATHA_API_URL = "http://host.containers.internal:8081/api_jsonrpc.php"  # visto de dentro do pod da Agatha
GROUP, HOST, TEMPLATE = "Agatha lab", "agatha-srv", "Linux by Zabbix agent"
RO_GROUP, RO_USER, TOKEN_NAME = "Agatha somente leitura", "agatha", "agatha-servidor"


class ZabbixError(Exception):
    pass


def call(method: str, params, auth: str | None = None):
    headers = {"Content-Type": "application/json-rpc"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    with urllib.request.urlopen(urllib.request.Request(API, data=body, headers=headers), timeout=30) as r:
        reply = json.load(r)
    if "error" in reply:
        raise ZabbixError(f"{method}: {reply['error'].get('data') or reply['error'].get('message')}")
    return reply["result"]


def wait_api() -> str:
    for _ in range(120):
        try:
            return call("apiinfo.version", {})
        except (urllib.error.URLError, ConnectionError, ZabbixError, json.JSONDecodeError):
            time.sleep(2)
    sys.exit("a API do Zabbix não respondeu (o pod está no ar? deploy/zabbix/up.sh)")


def admin_session() -> str:
    pw_file = LAB / "admin-password"
    if pw_file.exists():
        return call("user.login", {"username": "Admin", "password": pw_file.read_text().strip()})
    session = call("user.login", {"username": "Admin", "password": "zabbix"})
    admin = call("user.get", {"filter": {"username": "Admin"}, "output": ["userid"]}, session)[0]
    new_pw = secrets.token_urlsafe(18)
    call("user.update", {"userid": admin["userid"], "current_passwd": "zabbix", "passwd": new_pw}, session)
    old = os.umask(0o077)
    try:
        pw_file.write_text(new_pw + "\n")
    finally:
        os.umask(old)
    print(f"senha do Admin trocada; guardada em {pw_file}")
    return call("user.login", {"username": "Admin", "password": new_pw})


def get_or_create(auth, method_get, method_create, filter_, create_params, id_field):
    found = call(method_get, {"filter": filter_, "output": [id_field]}, auth)
    if found:
        return found[0][id_field], False
    return call(method_create, create_params, auth)[f"{id_field}s"][0], True


def update_env(values: dict) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)


def main() -> None:
    print(f"API do Zabbix {wait_api()}")
    auth = admin_session()

    group_id, new = get_or_create(auth, "hostgroup.get", "hostgroup.create", {"name": GROUP}, {"name": GROUP}, "groupid")
    print(f"grupo {GROUP}: {'criado' if new else 'já existia'}")

    template = call("template.get", {"filter": {"host": TEMPLATE}, "output": ["templateid"]}, auth)
    if not template:
        sys.exit(f"template {TEMPLATE!r} não encontrado")
    _, new = get_or_create(auth, "host.get", "host.create", {"host": HOST}, {
        "host": HOST,
        "groups": [{"groupid": group_id}],
        "templates": [{"templateid": template[0]["templateid"]}],
        "interfaces": [{"type": 1, "main": 1, "useip": 0, "ip": "", "dns": "host.containers.internal", "port": "10050"}],
    }, "hostid")
    print(f"host {HOST}: {'criado' if new else 'já existia'} (template {TEMPLATE})")

    default = call("host.get", {"filter": {"host": "Zabbix server"}, "output": ["hostid", "status"]}, auth)
    if default and default[0]["status"] == "0":
        call("host.update", {"hostid": default[0]["hostid"], "status": 1}, auth)
        print("host padrão 'Zabbix server' desativado")

    ro_group_id, new = get_or_create(auth, "usergroup.get", "usergroup.create", {"name": RO_GROUP}, {
        "name": RO_GROUP, "gui_access": 3,  # só API, sem acesso ao frontend
        "hostgroup_rights": [{"id": group_id, "permission": 2}],  # 2 = leitura
    }, "usrgrpid")
    print(f"grupo de usuários {RO_GROUP}: {'criado' if new else 'já existia'} (somente leitura em {GROUP})")

    role = call("role.get", {"filter": {"name": "User role"}, "output": ["roleid"]}, auth)[0]
    user_id, new = get_or_create(auth, "user.get", "user.create", {"username": RO_USER}, {
        "username": RO_USER, "passwd": secrets.token_urlsafe(24), "roleid": role["roleid"],
        "usrgrps": [{"usrgrpid": ro_group_id}],
    }, "userid")
    print(f"usuário {RO_USER}: {'criado' if new else 'já existia'}")

    # o texto do token só aparece ao gerar: recria a cada execução
    old_tokens = call("token.get", {"filter": {"name": TOKEN_NAME}, "userids": [user_id], "output": ["tokenid"]}, auth)
    if old_tokens:
        call("token.delete", [t["tokenid"] for t in old_tokens], auth)
    token_id = call("token.create", {"name": TOKEN_NAME, "userid": user_id}, auth)["tokenids"][0]
    token = call("token.generate", [token_id], auth)[0]["token"]
    update_env({"AGATHA_ZABBIX_URL": AGATHA_API_URL, "AGATHA_ZABBIX_TOKEN": token})
    print(f"token de API do usuário {RO_USER} gravado em {ENV_FILE}")

    # confere que o token só lê
    hosts = call("host.get", {"output": ["host"]}, token)
    print(f"token ok: enxerga {[h['host'] for h in hosts]}")
    try:
        call("host.update", {"hostid": "0", "status": 1}, token)
        print("ATENÇÃO: o token conseguiu chamar host.update")
    except ZabbixError as e:
        print(f"token não altera nada (host.update recusado: {str(e)[:60]})")


if __name__ == "__main__":
    main()
