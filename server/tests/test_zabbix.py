"""Ferramentas do Zabbix com a API simulada (httpx.MockTransport), sem depender do laboratório."""

import asyncio
import json
import time

import httpx
import pytest

from agatha_server.tools.zabbix import ZabbixClient, ZabbixConfig, _uptime, zabbix_tools

NOW = int(time.time())
STALE = False  # simula agente fora do ar: só valores velhos
MEMORY_KEY = "vm.memory.util"  # chave do template do Zabbix 7.0
EXTRA_HOSTS: list[dict] = []  # outros hosts monitorados, além do agatha-srv


def fake_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer t0k3n"
    body = json.loads(request.content)
    method, params = body["method"], body["params"]
    if method == "host.get":
        hosts = [{"hostid": "10", "host": "agatha-srv", "name": "agatha-srv",
                  "interfaces": [{"available": "1", "error": ""}]}, *EXTRA_HOSTS]
        term = (params.get("search") or {}).get("host")
        result = [h for h in hosts if term is None or term in h["host"]]
    elif method == "trigger.get":
        result = [{"description": "Uso de disco em /dados acima de 80%", "priority": "3", "lastchange": str(NOW - 60),
                   "hosts": [{"host": "agatha-srv", "name": "agatha-srv"}]}]
        if params.get("hostids") == ["999"]:
            result = []
    elif method == "item.get" and "filter" in params and STALE:
        result = [{"key_": "system.cpu.util", "lastvalue": "25.5", "lastclock": str(NOW - 900)}]
    elif method == "item.get" and STALE:
        result = []
    elif method == "item.get" and "filter" in params:
        result = [{"key_": "system.cpu.util", "lastvalue": "23.456", "lastclock": str(NOW)},
                  {"key_": MEMORY_KEY, "lastvalue": "61.04", "lastclock": str(NOW)},
                  {"key_": "system.uptime", "lastvalue": "190000", "lastclock": str(NOW)}]
        result = [i for i in result if i["key_"] in params["filter"]["key_"]]
    elif method == "item.get":
        result = [{"key_": "vfs.fs.dependent.size[/dados,pused]", "lastvalue": "41.27", "lastclock": str(NOW)},
                  {"key_": "vfs.fs.dependent.size[/boot,pused]", "lastvalue": "", "lastclock": "0"}]
    else:
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"message": "x", "data": f"{method}?"}, "id": 1})
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})


def tools():
    client = ZabbixClient(ZabbixConfig(url="http://zbx/api_jsonrpc.php", token="t0k3n"), transport=httpx.MockTransport(fake_api))
    problemas, host = zabbix_tools(client)
    return problemas, host


def test_problemas():
    problemas, _ = tools()
    result = asyncio.run(problemas.handler({}))
    p = result["problemas"][0]
    assert p["host"] == "agatha-srv" and p["severidade"] == "média" and "disco" in p["descricao"]
    assert "h" in p["desde"]  # hoje: "21h02"


def test_problemas_de_host_inexistente():
    problemas, _ = tools()
    assert "não achei" in asyncio.run(problemas.handler({"host": "xpto"}))["erro"]


def test_estado_do_host_por_nome_aproximado():
    _, host = tools()
    result = asyncio.run(host.handler({"host": "servidor agatha"}))
    assert result == {"host": "agatha-srv", "disponivel": True, "cpu_pct": 23.5, "memoria_pct": 61.0,
                      "ligado_ha": "2 dias e 4 horas", "disco_pct": {"/dados": 41.3}}


def test_memoria_com_a_chave_dos_templates_antigos():
    global MEMORY_KEY
    MEMORY_KEY = "vm.memory.utilization"
    try:
        _, host = tools()
        assert asyncio.run(host.handler({"host": "agatha-srv"}))["memoria_pct"] == 61.0
    finally:
        MEMORY_KEY = "vm.memory.util"


@pytest.mark.parametrize("seconds,expected", [
    (190000, "2 dias e 4 horas"), (90000, "1 dia e 1 hora"), (7300, "2 horas"), (720, "12 minutos"),
])
def test_tempo_ligado(seconds, expected):
    assert _uptime(seconds) == expected


def test_host_desconhecido_lista_os_monitorados():
    _, host = tools()
    result = asyncio.run(host.handler({"host": "banco-prod"}))
    assert result["hosts_monitorados"] == ["agatha-srv"]


def test_desligado_sem_configuracao():
    assert not ZabbixConfig().enabled
    assert ZabbixConfig(url="u", token="t").enabled


def test_host_com_nome_trocado_pelo_stt_acha_por_semelhanca():
    _, host = tools()
    assert asyncio.run(host.handler({"host": "Agatha CRV"}))["host"] == "agatha-srv"


def test_host_por_parte_do_nome_so_se_for_unico():
    # teste de 12/10: "agatha-srv" chegou como "Agatha, server" e o modelo pediu só "server"
    _, host = tools()
    assert asyncio.run(host.handler({"host": "server"}))["host"] == "agatha-srv"
    EXTRA_HOSTS.append({"hostid": "11", "host": "web-srv", "name": "web-srv", "interfaces": [{"available": "1"}]})
    try:
        assert "hosts_monitorados" in asyncio.run(host.handler({"host": "server"}))  # ambíguo: pergunta
    finally:
        EXTRA_HOSTS.clear()


def test_valor_velho_nao_e_apresentado_como_atual():
    global STALE
    STALE = True
    try:
        _, host = tools()
        result = asyncio.run(host.handler({"host": "agatha"}))
    finally:
        STALE = False
    assert "cpu_pct" not in result
    assert result["sem_dados_recentes"].startswith("última coleta")
