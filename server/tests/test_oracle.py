"""Ferramentas do Oracle com a consulta simulada (sem banco)."""

import asyncio
from datetime import datetime, timedelta

from agatha_server.tools.oracle import (BACKUPS_SQL, DATABASE_SQL, DATAGUARD_SQL, LOCKS_SQL, SESSIONS_SQL,
                                        TABLESPACES_SQL, OracleConfig, oracle_tools)

YESTERDAY = (datetime.now() - timedelta(days=1)).replace(hour=2, minute=31, second=0, microsecond=0)


async def fake_query(sql: str):
    if sql == TABLESPACES_SQL:
        return [("DADOS_APP", 85.2, 55, 64), ("SYSTEM", 74.0, 520, 32767)]
    if sql == SESSIONS_SQL:
        return [(7, 2)]
    if sql == DATABASE_SQL:
        return [("ARCHIVELOG", "READ WRITE", "PRIMARY", "OPEN", "23.9.0.25.07", 100.4)]
    if sql == LOCKS_SQL:
        return [(3, 1, 125)]
    raise AssertionError(sql)


async def fake_cdb_query(sql: str):
    if sql == DATABASE_SQL:
        return await fake_query(sql)
    if sql == DATAGUARD_SQL:
        return [(0, 0)]
    if sql == BACKUPS_SQL:
        return [(YESTERDAY, "DB FULL", "COMPLETED", 437 * 1048576, 70),
                (YESTERDAY - timedelta(days=1), "DB FULL", "FAILED", 0, 12)]
    raise AssertionError(sql)


async def failing_query(sql: str):
    raise ConnectionError("DPY-6005: cannot connect to database")


def by_name(query=fake_query, cdb_query=None):
    return {t.name: t for t in oracle_tools(query, cdb_query)}


def test_tablespaces():
    assert asyncio.run(by_name()["oracle_tablespaces"].handler({})) == {"tablespaces": [
        {"nome": "DADOS_APP", "usado_pct": 85.2, "usado": "55 MB", "livre": "9 MB", "maximo": "64 MB"},
        {"nome": "SYSTEM", "usado_pct": 74.0, "usado": "520 MB", "livre": "31 GB", "maximo": "32 GB"},
    ]}


def test_sessoes():
    assert asyncio.run(by_name()["oracle_sessoes"].handler({})) == {"total": 7, "ativas": 2}


def test_falha_de_conexao_vira_erro_explicavel():
    tools = by_name(failing_query, failing_query)
    assert asyncio.run(tools["oracle_tablespaces"].handler({}))["erro"].startswith("não consegui consultar o Oracle: DPY-6005")
    for name in ("oracle_sessoes", "oracle_banco", "oracle_backups"):
        assert "erro" in asyncio.run(tools[name].handler({}))


def test_estado_do_banco_com_locks_e_data_guard():
    result = asyncio.run(by_name(cdb_query=fake_cdb_query)["oracle_banco"].handler({}))
    assert result == {"instancia": "aberta", "modo": "leitura e escrita", "modo_archivelog": "ligado", "papel": "primário",
                      "versao": "23.9.0.25.07", "no_ar_ha": "4 dias", "sessoes_bloqueadas": 3,
                      "sessoes_bloqueando": 1, "maior_espera": "2 minutos", "data_guard": "não configurado: o banco não tem standby"}


def test_estado_do_banco_sem_conexao_do_cdb():
    result = asyncio.run(by_name()["oracle_banco"].handler({}))
    assert result["modo_archivelog"] == "ligado" and "data_guard" not in result


def test_backups_do_rman():
    result = asyncio.run(by_name(cdb_query=fake_cdb_query)["oracle_backups"].handler({}))
    first, second = result["backups"]
    assert first == {"inicio": "ontem às 02h31", "tipo": "completo", "status": "concluído",
                     "tamanho": "437 MB", "duracao": "1 minuto"}
    assert second["status"] == "falhou" and second["inicio"] == f"{YESTERDAY - timedelta(days=1):%d/%m} às 02h31"
    assert result["ultimo_concluido_ha"].endswith("horas")


def test_sem_conexao_do_cdb_nao_tem_backups():
    assert "oracle_backups" not in by_name()
    assert "oracle_backups" in by_name(cdb_query=fake_cdb_query)


def test_desligado_sem_configuracao():
    assert not OracleConfig().enabled
    assert OracleConfig(dsn="d", user="u", password="p").enabled
    assert not OracleConfig(dsn="d", user="u", password="p").cdb_enabled


def test_tamanhos_legiveis():
    from agatha_server.tools.oracle import _human
    assert [_human(9), _human(904809), _human(33554432)] == ["9 MB", "884 GB", "32 TB"]
