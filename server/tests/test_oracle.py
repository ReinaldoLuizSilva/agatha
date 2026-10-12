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
        {"nome": "DADOS_APP", "usado_pct": 85.2, "usado": "55 MB", "livre": "9 MB", "maximo": "64 MB"},
        {"nome": "SYSTEM", "usado_pct": 74.0, "usado": "520 MB", "livre": "31 GB", "maximo": "32 GB"},
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


def test_tamanhos_legiveis():
    from agatha_server.tools.oracle import _human
    assert [_human(9), _human(904809), _human(33554432)] == ["9 MB", "884 GB", "32 TB"]
