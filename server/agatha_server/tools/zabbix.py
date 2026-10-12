"""Ferramentas do Zabbix (ADR-011): somente leitura, pela API com o token do usuário "agatha".

- zabbix_problemas(host?): triggers em estado de problema agora, com severidade e desde quando;
- zabbix_host(host): disponibilidade do agente e métricas atuais (CPU, memória, discos, uptime).

O token pertence a um usuário com permissão só de leitura (deploy/zabbix/setup.py).
"""

import difflib
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from .base import Example, Tool

log = logging.getLogger(__name__)

SEVERITIES = ["não classificada", "informação", "atenção", "média", "alta", "desastre"]
STALE_S = 300  # valor mais velho que isso não é "atual" (ex.: agente fora do ar)
# Uso de memória em %: o template "Linux by Zabbix agent" do 7.0 tem vm.memory.util (dependente de
# vm.memory.size[pavailable]); templates antigos usam a chave nativa do agente, vm.memory.utilization.
MEMORY_KEYS = ("vm.memory.util", "vm.memory.utilization")


@dataclass(frozen=True)
class ZabbixConfig:
    url: str = ""    # ex.: http://host.containers.internal:8081/api_jsonrpc.php
    token: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)


class ZabbixError(Exception):
    pass


class ZabbixClient:
    def __init__(self, config: ZabbixConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0), transport=transport,
            headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json-rpc"},
        )
        self._url = config.url

    async def call(self, method: str, params: dict):
        response = await self._client.post(self._url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
        response.raise_for_status()
        reply = response.json()
        if "error" in reply:
            raise ZabbixError(reply["error"].get("data") or reply["error"].get("message"))
        return reply["result"]

    async def find_hosts(self, name: str) -> list[dict]:
        """Busca por nome técnico ou visível, sem exigir o nome exato ("agatha" acha "agatha-srv").

        Se a busca por trecho não achar nada, tenta por semelhança: o nome chega pelo reconhecimento
        de voz e pode vir trocado ("agatha crv" → "agatha-srv").
        """
        term = name.strip().lower().replace("servidor ", "").replace(" ", "-")
        fields = {"output": ["hostid", "host", "name"], "selectInterfaces": ["available", "error"]}
        found = await self.call("host.get", {**fields, "search": {"host": term, "name": term}, "searchByAny": True})
        if found:
            return found
        hosts = await self.call("host.get", fields)
        close = difflib.get_close_matches(term, [h["host"].lower() for h in hosts], n=1, cutoff=0.6)
        return [h for h in hosts if close and h["host"].lower() == close[0]]

    async def aclose(self) -> None:
        await self._client.aclose()


def _since(clock: str | int, now: datetime | None = None) -> str:
    when = datetime.fromtimestamp(int(clock))
    now = now or datetime.now()
    return f"{when.hour}h{when.minute:02d}" if when.date() == now.date() else f"{when:%d/%m} às {when.hour}h{when.minute:02d}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours = rest // 3600
    if days:
        return f"{_plural(days, 'dia')} e {_plural(hours, 'hora')}"
    # recém-ligado: "0 horas" (teste de 12/10) não diz nada
    return _plural(hours, "hora") if hours else _plural(int(seconds) // 60, "minuto")


def zabbix_tools(client: ZabbixClient) -> list[Tool]:
    async def problemas(args: dict) -> dict:
        params = {"output": ["description", "priority", "lastchange"], "only_true": True, "monitored": True,
                  "active": True, "skipDependent": True, "filter": {"value": 1}, "expandDescription": True,
                  "selectHosts": ["host", "name"], "sortfield": "priority", "sortorder": "DESC", "limit": 10}
        host = (args.get("host") or "").strip()
        if host:
            hosts = await client.find_hosts(host)
            if not hosts:
                return {"erro": f"não achei nenhum host parecido com {host!r} no Zabbix"}
            params["hostids"] = [h["hostid"] for h in hosts]
        triggers = await client.call("trigger.get", params)
        return {"problemas": [
            {"host": t["hosts"][0]["name"] if t.get("hosts") else "?", "severidade": SEVERITIES[int(t["priority"])],
             "descricao": t["description"], "desde": _since(t["lastchange"])}
            for t in triggers
        ]}

    async def host_status(args: dict) -> dict:
        name = (args.get("host") or "").strip()
        hosts = await client.find_hosts(name) if name else []
        if not hosts:
            known = [h["name"] for h in await client.call("host.get", {"output": ["name"]})]
            return {"erro": f"não achei o host {name!r}", "hosts_monitorados": known}
        host = hosts[0]
        items = await client.call("item.get", {
            "hostids": [host["hostid"]], "output": ["key_", "lastvalue", "lastclock"],
            "filter": {"key_": ["system.cpu.util", *MEMORY_KEYS, "system.uptime"]},
        })
        disks = await client.call("item.get", {
            "hostids": [host["hostid"]], "output": ["key_", "lastvalue", "lastclock"],
            "search": {"key_": ",pused]"},
        })
        now = datetime.now().timestamp()

        def fresh(item) -> bool:
            return item["lastvalue"] != "" and now - int(item["lastclock"]) <= STALE_S

        values = {i["key_"]: float(i["lastvalue"]) for i in items if fresh(i)}
        last = max((int(i["lastclock"]) for i in [*items, *disks]), default=0)
        iface = (host.get("interfaces") or [{}])[0]
        result = {"host": host["name"], "disponivel": iface.get("available") == "1"}
        if iface.get("available") == "2":
            result["erro_agente"] = iface.get("error", "")
        if not values and last:
            # sem dado recente: não apresentar o último valor como se fosse atual
            result["sem_dados_recentes"] = f"última coleta {_since(last)}"
        if "system.cpu.util" in values:
            result["cpu_pct"] = round(values["system.cpu.util"], 1)
        if (memory := next((values[k] for k in MEMORY_KEYS if k in values), None)) is not None:
            result["memoria_pct"] = round(memory, 1)
        if "system.uptime" in values:
            result["ligado_ha"] = _uptime(values["system.uptime"])
        fs = {}
        for d in disks:
            if fresh(d):
                mount = d["key_"].split("[", 1)[1].split(",", 1)[0]
                fs[mount] = round(float(d["lastvalue"]), 1)
        if fs:
            result["disco_pct"] = fs
        return result

    return [
        Tool(
            name="zabbix_problemas",
            description="Lista os alertas (problemas) ativos no Zabbix, de todos os hosts ou de um host.",
            handler=problemas,
            rule="perguntas sobre alertas ou problemas atuais no Zabbix: use zabbix_problemas",
            properties={"host": {"type": "string", "description": "nome do host; omita para todos"}},
            examples=(Example("Tem algum alerta no Zabbix?", {}, {"problemas": []}, "Nenhum alerta ativo agora."),),
        ),
        Tool(
            name="zabbix_host",
            description="Mostra o estado e as métricas atuais de um host monitorado no Zabbix: CPU, memória, disco.",
            handler=host_status,
            rule="perguntas sobre o estado atual de um host no Zabbix (CPU, memória, disco): use zabbix_host",
            properties={"host": {"type": "string"}},
            required=("host",),
        ),
    ]
