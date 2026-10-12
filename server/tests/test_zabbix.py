"""Ferramentas do Zabbix com a API simulada (httpx.MockTransport), sem depender do laboratório."""

import asyncio
import json
import time

import httpx

from agatha_server.tools.zabbix import ZabbixClient, ZabbixConfig, zabbix_tools

NOW = int(time.time())


def fake_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer t0k3n"
    body = json.loads(request.content)
    method, params = body["method"], body["params"]
    if method == "host.get":
        hosts = [{"hostid": "10", "host": "agatha-srv", "name": "agatha-srv",
                  "interfaces": [{"available": "1", "error": ""}]}]
        term = (params.get("search") or {}).get("host")
        result = [h for h in hosts if term is None or term in h["host"]]
    elif method == "trigger.get":
        result = [{"description": "Uso de disco em /dados acima de 80%", "priority": "3", "lastchange": str(NOW - 60),
                   "hosts": [{"host": "agatha-srv", "name": "agatha-srv"}]}]
        if params.get("hostids") == ["999"]:
            result = []
    elif method == "item.get" and "filter" in params:
        result = [{"key_": "system.cpu.util", "lastvalue": "23.456", "lastclock": str(NOW)},
                  {"key_": "vm.memory.utilization", "lastvalue": "61.04", "lastclock": str(NOW)},
                  {"key_": "system.uptime", "lastvalue": "190000", "lastclock": str(NOW)}]
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
