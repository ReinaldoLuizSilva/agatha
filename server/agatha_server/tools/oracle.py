"""Ferramentas do Oracle (ADR-012): somente leitura, com o usuário AGATHA_RO.

- oracle_tablespaces(): uso de cada tablespace (considera o limite de autoextend);
- oracle_sessoes(): sessões de usuário abertas e ativas.

O AGATHA_RO só tem CREATE SESSION e SELECT nessas views (deploy/oracle/setup.py).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import Example, Tool

log = logging.getLogger(__name__)

Query = Callable[[str], Awaitable[list[tuple]]]

TABLESPACES_SQL = """
SELECT m.tablespace_name,
       ROUND(m.used_percent, 1),
       ROUND(m.used_space * t.block_size / 1048576),
       ROUND(m.tablespace_size * t.block_size / 1048576)
  FROM dba_tablespace_usage_metrics m
  JOIN dba_tablespaces t ON t.tablespace_name = m.tablespace_name
 ORDER BY m.used_percent DESC
"""

SESSIONS_SQL = """
SELECT COUNT(*), NVL(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0)
  FROM v$session
 WHERE type = 'USER'
"""


@dataclass(frozen=True)
class OracleConfig:
    dsn: str = ""       # ex.: host.containers.internal:1521/FREEPDB1
    user: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.dsn and self.user and self.password)


class OracleClient:
    """Conexões assíncronas (python-oracledb, modo thin: sem Instant Client), num pool pequeno."""

    def __init__(self, config: OracleConfig):
        self.config = config
        self._pool = None

    async def query(self, sql: str) -> list[tuple]:
        import oracledb

        if self._pool is None:
            self._pool = oracledb.create_pool_async(
                user=self.config.user, password=self.config.password, dsn=self.config.dsn,
                min=0, max=2, increment=1, tcp_connect_timeout=5,
            )
        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(sql)
                return await cur.fetchall()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close(force=True)


def _db_error(e: Exception) -> dict:
    first = str(e).splitlines()[0] if str(e) else type(e).__name__
    return {"erro": f"não consegui consultar o Oracle: {first}"}


def oracle_tools(query: Query) -> list[Tool]:
    async def tablespaces(args: dict) -> dict:
        try:
            rows = await query(TABLESPACES_SQL)
        except Exception as e:
            log.warning("oracle_tablespaces falhou: %s", e)
            return _db_error(e)
        return {"tablespaces": [
            # limite em GB: com autoextend ilimitado ele vira o espaço livre do disco (centenas de
            # milhares de MB), ruim de ouvir
            {"nome": name, "usado_pct": float(pct), "usado_mb": int(used), "limite_gb": round(int(size) / 1024, 1)}
            for name, pct, used, size in rows
        ]}

    async def sessoes(args: dict) -> dict:
        try:
            (total, active), = await query(SESSIONS_SQL)
        except Exception as e:
            log.warning("oracle_sessoes falhou: %s", e)
            return _db_error(e)
        return {"total": int(total), "ativas": int(active)}

    return [
        Tool(
            name="oracle_tablespaces",
            description="Mostra o uso de espaço das tablespaces do banco Oracle.",
            handler=tablespaces,
            rule="perguntas sobre o uso atual das tablespaces do Oracle: use oracle_tablespaces",
            examples=(Example("Como está o uso das tablespaces?", {},
                              {"tablespaces": [{"nome": "DADOS", "usado_pct": 52}]},
                              "Tudo tranquilo. A DADOS está com 52%."),),
        ),
        Tool(
            name="oracle_sessoes",
            description="Mostra quantas sessões estão abertas no banco Oracle e quantas estão ativas.",
            handler=sessoes,
            rule="perguntas sobre sessões abertas no Oracle: use oracle_sessoes",
        ),
    ]
