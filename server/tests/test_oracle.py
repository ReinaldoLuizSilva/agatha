"""Ferramentas do Oracle com a consulta simulada (sem banco)."""

import asyncio

from agatha_server.tools.oracle import SESSIONS_SQL, TABLESPACES_SQL, OracleConfig, oracle_tools


async def fake_query(sql: str):
    if sql == TABLESPACES_SQL:
        return [("DADOS_APP", 85.2, 55, 64), ("SYSTEM", 74.0, 520, 32767)]
    if sql == SESSIONS_SQL:
        return [(7, 2)]
    raise AssertionError(sql)


async def failing_query(sql: str):
    raise ConnectionError("DPY-6005: cannot connect to database")


def test_tablespaces():
    tablespaces, _ = oracle_tools(fake_query)
    assert asyncio.run(tablespaces.handler({})) == {"tablespaces": [
        {"nome": "DADOS_APP", "usado_pct": 85.2, "usado_mb": 55, "limite_gb": 0.1},
        {"nome": "SYSTEM", "usado_pct": 74.0, "usado_mb": 520, "limite_gb": 32.0},
    ]}


def test_sessoes():
    _, sessoes = oracle_tools(fake_query)
    assert asyncio.run(sessoes.handler({})) == {"total": 7, "ativas": 2}


def test_falha_de_conexao_vira_erro_explicavel():
    tablespaces, sessoes = oracle_tools(failing_query)
    assert asyncio.run(tablespaces.handler({}))["erro"].startswith("não consegui consultar o Oracle: DPY-6005")
    assert "erro" in asyncio.run(sessoes.handler({}))


def test_desligado_sem_configuracao():
    assert not OracleConfig().enabled
    assert OracleConfig(dsn="d", user="u", password="p").enabled
