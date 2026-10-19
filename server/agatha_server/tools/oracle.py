"""Ferramentas do Oracle (ADR-012, ADR-020): somente leitura.

- oracle_tablespaces(): uso de cada tablespace (considera o limite de autoextend);
- oracle_sessoes(): sessões de usuário abertas e ativas;
- oracle_banco(): estado do banco (aberto, archivelog, papel, Data Guard, versão, no ar há) e locks;
- oracle_backups(): últimos jobs do RMAN.

Duas conexões (deploy/oracle/setup.py): AGATHA_RO na PDB (tablespaces, sessões, locks) e o usuário
comum C##AGATHA_RO no contêiner raiz (estado do banco, Data Guard e backups: o RMAN registra no
controlfile, que a PDB não enxerga). Os dois só têm CREATE SESSION e SELECT nas views usadas.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

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

DATABASE_SQL = """
SELECT d.log_mode, d.open_mode, d.database_role, i.status, i.version_full,
       ROUND((SYSDATE - i.startup_time) * 24, 1)
  FROM v$database d CROSS JOIN v$instance i
"""

LOCKS_SQL = """
SELECT COUNT(*), COUNT(DISTINCT blocking_session), NVL(MAX(seconds_in_wait), 0)
  FROM v$session
 WHERE blocking_session IS NOT NULL
"""

# Destinos de redo que não são locais: os standbys do Data Guard (no primário)
DATAGUARD_SQL = """
SELECT COUNT(*), NVL(SUM(CASE WHEN status = 'VALID' AND gap_status = 'NO GAP' THEN 1 ELSE 0 END), 0)
  FROM v$archive_dest_status
 WHERE type <> 'LOCAL' AND status <> 'INACTIVE'
"""

BACKUPS_SQL = """
SELECT start_time, input_type, status, output_bytes, elapsed_seconds
  FROM v$rman_backup_job_details
 ORDER BY start_time DESC
 FETCH FIRST 5 ROWS ONLY
"""

ROLES = {"PRIMARY": "primário", "PHYSICAL STANDBY": "standby físico", "LOGICAL STANDBY": "standby lógico",
         "SNAPSHOT STANDBY": "standby snapshot"}
BACKUP_TYPES = {"DB FULL": "completo", "DB INCR": "incremental", "ARCHIVELOG": "archivelogs",
                "DATAFILE FULL": "de datafiles", "CONTROLFILE": "do controlfile", "SPFILE": "do spfile"}
BACKUP_STATUS = {"COMPLETED": "concluído", "FAILED": "falhou", "RUNNING": "rodando agora",
                 "COMPLETED WITH WARNINGS": "concluído com avisos", "COMPLETED WITH ERRORS": "concluído com erros",
                 "RUNNING WITH WARNINGS": "rodando, com avisos", "RUNNING WITH ERRORS": "rodando, com erros"}


@dataclass(frozen=True)
class OracleConfig:
    dsn: str = ""       # ex.: host.containers.internal:1521/FREEPDB1
    user: str = ""
    password: str = ""
    # contêiner raiz (ADR-020), ex.: host.containers.internal:1521/FREE com C##AGATHA_RO
    cdb_dsn: str = ""
    cdb_user: str = ""
    cdb_password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.dsn and self.user and self.password)

    @property
    def cdb_enabled(self) -> bool:
        return bool(self.cdb_dsn and self.cdb_user and self.cdb_password)


class OracleClient:
    """Conexões assíncronas (python-oracledb, modo thin: sem Instant Client), num pool pequeno.
    `cdb=True` usa a conexão do contêiner raiz."""

    def __init__(self, config: OracleConfig, cdb: bool = False):
        self.config = config
        self._login = ((config.cdb_user, config.cdb_password, config.cdb_dsn) if cdb
                       else (config.user, config.password, config.dsn))
        self._pool = None

    async def query(self, sql: str) -> list[tuple]:
        import oracledb

        if self._pool is None:
            user, password, dsn = self._login
            self._pool = oracledb.create_pool_async(
                user=user, password=password, dsn=dsn, min=0, max=2, increment=1, tcp_connect_timeout=5,
            )
        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(sql)
                return await cur.fetchall()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close(force=True)


def _human(mb: int) -> str:
    """Tamanho com a unidade que soa natural ("9 MB", "883 GB", "32 TB"), pronto para a fala."""
    if mb < 1024:
        return f"{mb} MB"
    if mb < 1024 * 1024:
        return f"{round(mb / 1024)} GB"
    return f"{round(mb / 1024 / 1024)} TB"


def _duration(hours: float) -> str:
    """"3 dias", "5 horas", "40 minutos": pronto para a fala."""
    if hours >= 48:
        return f"{round(hours / 24)} dias"
    if hours >= 2:
        return f"{round(hours)} horas"
    minutes = round(hours * 60)
    return "1 minuto" if minutes <= 1 else f"{minutes} minutos"


def _when(moment: datetime, now: datetime) -> str:
    """"hoje às 02h31", "ontem às 02h31", "17/10 às 02h31": como se fala ("às 18/10 às" soava mal)."""
    days = (now.date() - moment.date()).days
    day = "hoje" if days == 0 else "ontem" if days == 1 else f"{moment:%d/%m}"
    return f"{day} às {moment:%Hh%M}"


def _db_error(e: Exception) -> dict:
    first = str(e).splitlines()[0] if str(e) else type(e).__name__
    return {"erro": f"não consegui consultar o Oracle: {first}"}


def oracle_tools(query: Query, cdb_query: Query | None = None) -> list[Tool]:
    """`query` conecta na PDB; `cdb_query`, no contêiner raiz (sem ele, não há oracle_backups e o
    estado do banco sai sem o Data Guard)."""
    async def tablespaces(args: dict) -> dict:
        try:
            rows = await query(TABLESPACES_SQL)
        except Exception as e:
            log.warning("oracle_tablespaces falhou: %s", e)
            return _db_error(e)
        # Espaço livre e máximo já calculados e com unidade: sem isso o modelo tentava fazer a conta
        # (e misturava MB com GB). Com autoextend ilimitado, o máximo é o espaço livre do disco.
        return {"tablespaces": [
            {"nome": name, "usado_pct": float(pct), "usado": _human(int(used)), "livre": _human(int(size) - int(used)),
             "maximo": _human(int(size))}
            for name, pct, used, size in rows
        ]}

    async def sessoes(args: dict) -> dict:
        try:
            (total, active), = await query(SESSIONS_SQL)
        except Exception as e:
            log.warning("oracle_sessoes falhou: %s", e)
            return _db_error(e)
        return {"total": int(total), "ativas": int(active)}

    async def banco(args: dict) -> dict:
        try:
            (log_mode, open_mode, role, status, version, hours), = await (cdb_query or query)(DATABASE_SQL)
            (blocked, blockers, waited), = await query(LOCKS_SQL)
            dataguard = await cdb_query(DATAGUARD_SQL) if cdb_query else None
        except Exception as e:
            log.warning("oracle_banco falhou: %s", e)
            return _db_error(e)
        result = {
            "instancia": "aberta" if status == "OPEN" else status.lower(),
            "modo": "leitura e escrita" if open_mode == "READ WRITE" else open_mode.lower(),
            # texto, não booleano: com true/false o modelo pulava o campo ("está em archivelog?" sem resposta)
            "modo_archivelog": "ligado" if log_mode == "ARCHIVELOG" else "desligado",
            "papel": ROLES.get(role, role.lower()),
            "versao": version,
            "no_ar_ha": _duration(float(hours)),
            "sessoes_bloqueadas": int(blocked),
        }
        if blocked:
            result["sessoes_bloqueando"] = int(blockers)
            result["maior_espera"] = _duration(float(waited) / 3600)
        if dataguard is not None:
            (standbys, in_sync), = dataguard
            result["data_guard"] = ("não configurado: o banco não tem standby" if not standbys
                                    else f"{int(standbys)} standby, {int(in_sync)} sincronizado sem atraso")
        return result

    async def backups(args: dict) -> dict:
        try:
            rows = await cdb_query(BACKUPS_SQL)
        except Exception as e:
            log.warning("oracle_backups falhou: %s", e)
            return _db_error(e)
        if not rows:
            return {"backups": [], "aviso": "nenhum backup do RMAN registrado"}
        now = datetime.now()
        result = {"backups": [
            {"inicio": _when(start, now), "tipo": BACKUP_TYPES.get(kind, kind.lower()),
             "status": BACKUP_STATUS.get(state, state.lower()), "tamanho": _human(round((size or 0) / 1048576)),
             "duracao": _duration(float(seconds or 0) / 3600)}
            for start, kind, state, size, seconds in rows
        ]}
        done = [start for start, _, state, _, _ in rows if state.startswith("COMPLETED")]
        if done:
            result["ultimo_concluido_ha"] = _duration((now - done[0]).total_seconds() / 3600)
        return result

    tools = [
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
        Tool(
            name="oracle_banco",
            description="Mostra o estado do banco Oracle: se está aberto, modo archivelog, papel e Data Guard, "
                        "versão, há quanto tempo está no ar e sessões bloqueadas por locks.",
            handler=banco,
            rule="perguntas sobre o estado do banco Oracle, como se está no ar, archivelog, Data Guard ou locks: "
                 "use oracle_banco",
        ),
    ]
    if cdb_query is not None:
        tools.append(Tool(
            name="oracle_backups",
            description="Lista os últimos backups do Oracle feitos pelo RMAN: quando, tipo, status e tamanho.",
            handler=backups,
            rule="perguntas sobre os backups do Oracle, como se o backup de ontem rodou: use oracle_backups",
        ))
    return tools
