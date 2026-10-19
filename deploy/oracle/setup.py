"""Configuração do Oracle de laboratório (ADR-012). Pode ser rodado de novo.

Conecta como SYS (SYSDBA) no PDB FREEPDB1 e:
1. cria (ou atualiza) o usuário agatha_ro com o mínimo de privilégio: CREATE SESSION e SELECT só
   nas views que as ferramentas da Agatha usam; senha aleatória em <lab>/agatha_ro-password (600);
2. cria a tablespace de demonstração DADOS_APP (64 MB, sem autoextend) e a enche até ~85%, para
   existir uma "tablespace quase cheia" de verdade;
3. confere que o agatha_ro lê as views e NÃO consegue criar nada;
4. grava AGATHA_ORACLE_DSN, AGATHA_ORACLE_USER e AGATHA_ORACLE_PASSWORD no server/.env.

Uso: .venv/bin/python deploy/oracle/setup.py
"""

import os
import secrets
import sys
from pathlib import Path

import oracledb

LAB = Path(os.environ.get("ORACLE_LAB_DIR", "/dados/agatha/lab/oracle"))
DSN = os.environ.get("ORACLE_DSN", "127.0.0.1:1521/FREEPDB1")
AGATHA_DSN = "host.containers.internal:1521/FREEPDB1"  # visto de dentro do pod da Agatha
ENV_FILE = Path(__file__).resolve().parents[2] / "server" / ".env"
RO_USER = "AGATHA_RO"
VIEWS = ["SYS.DBA_TABLESPACE_USAGE_METRICS", "SYS.DBA_TABLESPACES", "SYS.V_$SESSION", "SYS.V_$DATABASE",
         "SYS.V_$INSTANCE"]
# Backups (ADR-020): o RMAN registra no controlfile, que só aparece no contêiner raiz (na PDB a
# V$RMAN_BACKUP_JOB_DETAILS vem vazia). Como os plugins de monitoração (ex.: o do Zabbix), um usuário
# comum só de leitura no CDB.
CDB_DSN = os.environ.get("ORACLE_CDB_DSN", "127.0.0.1:1521/FREE")
AGATHA_CDB_DSN = "host.containers.internal:1521/FREE"
CDB_USER = "C##AGATHA_RO"
CDB_VIEWS = ["SYS.V_$RMAN_BACKUP_JOB_DETAILS", "SYS.V_$DATABASE", "SYS.V_$INSTANCE", "SYS.V_$ARCHIVE_DEST_STATUS"]
DEMO_TS, DEMO_USER, TARGET_PCT = "DADOS_APP", "APP_DEMO", 85


def secret_file(name: str) -> str:
    path = LAB / name
    if not path.exists():
        old = os.umask(0o077)
        try:
            # começa com letra e só tem letras/números: aceito sem aspas em qualquer cliente
            path.write_text("A" + secrets.token_hex(12) + "\n")
        finally:
            os.umask(old)
    return path.read_text().strip()


def exists(cur, sql: str, **binds) -> bool:
    cur.execute(sql, binds)
    return cur.fetchone()[0] > 0


def used_pct(cur, tablespace: str) -> float:
    cur.execute("SELECT used_percent FROM dba_tablespace_usage_metrics WHERE tablespace_name = :t", t=tablespace)
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


def update_env(values: dict) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)


def main() -> None:
    system_pw = (LAB / "system-password").read_text().strip()
    ro_pw = secret_file("agatha_ro-password")
    # SYS como SYSDBA: no 23ai, o SYSTEM não consegue conceder SELECT nas views do SYS (ORA-01031)
    with oracledb.connect(user="sys", password=system_pw, dsn=DSN, mode=oracledb.AUTH_MODE_SYSDBA) as conn:
        cur = conn.cursor()
        cur.execute("SELECT banner_full FROM v$version")
        print(cur.fetchone()[0].splitlines()[0])

        if exists(cur, "SELECT COUNT(*) FROM dba_users WHERE username = :u", u=RO_USER):
            cur.execute(f'ALTER USER {RO_USER} IDENTIFIED BY "{ro_pw}"')
            print(f"usuário {RO_USER}: já existia (senha atualizada)")
        else:
            cur.execute(f'CREATE USER {RO_USER} IDENTIFIED BY "{ro_pw}"')
            print(f"usuário {RO_USER}: criado")
        cur.execute(f"GRANT CREATE SESSION TO {RO_USER}")
        for view in VIEWS:
            cur.execute(f"GRANT SELECT ON {view} TO {RO_USER}")
        print(f"  privilégios: CREATE SESSION + SELECT em {', '.join(v.split('.')[1] for v in VIEWS)}")

        if not exists(cur, "SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = :t", t=DEMO_TS):
            # sem OMF (db_create_file_dest vazio): usa o diretório dos datafiles que o PDB já tem
            cur.execute("SELECT file_name FROM dba_data_files WHERE ROWNUM = 1")
            folder = cur.fetchone()[0].rsplit("/", 1)[0]
            cur.execute(f"CREATE TABLESPACE {DEMO_TS} DATAFILE '{folder}/dados_app01.dbf' SIZE 64M AUTOEXTEND OFF")
            print(f"tablespace {DEMO_TS}: criada (64 MB, sem autoextend)")
        if not exists(cur, "SELECT COUNT(*) FROM dba_users WHERE username = :u", u=DEMO_USER):
            cur.execute(f'CREATE USER {DEMO_USER} IDENTIFIED BY "{secrets.token_hex(12)}x" '
                        f"DEFAULT TABLESPACE {DEMO_TS} QUOTA UNLIMITED ON {DEMO_TS} ACCOUNT LOCK")
            cur.execute(f"CREATE TABLE {DEMO_USER}.pedidos (id NUMBER, cliente VARCHAR2(100), detalhe VARCHAR2(2000)) "
                        f"TABLESPACE {DEMO_TS}")
        # enche em blocos de ~4 MB até a meta (a demonstração de "tablespace quase cheia")
        while (pct := used_pct(cur, DEMO_TS)) < TARGET_PCT:
            cur.execute(f"INSERT /*+ APPEND */ INTO {DEMO_USER}.pedidos "
                        "SELECT level, 'cliente ' || level, RPAD('x', 1900, 'x') FROM dual CONNECT BY level <= 2000")
            conn.commit()
        print(f"tablespace {DEMO_TS}: {pct:.1f}% usada")

    # confere o usuário somente leitura
    with oracledb.connect(user=RO_USER, password=ro_pw, dsn=DSN) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dba_tablespace_usage_metrics")
        n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM v$session WHERE type = 'USER'")
        print(f"{RO_USER} lê as views: {n} tablespaces, {cur.fetchone()[0]} sessões de usuário")
        try:
            cur.execute("CREATE TABLE teste_escrita (x NUMBER)")
            sys.exit(f"ATENÇÃO: {RO_USER} conseguiu criar uma tabela")
        except oracledb.DatabaseError as e:
            print(f"{RO_USER} não cria nada ({str(e).splitlines()[0]})")

    cdb_pw = setup_cdb_user(system_pw)
    update_env({"AGATHA_ORACLE_DSN": AGATHA_DSN, "AGATHA_ORACLE_USER": RO_USER, "AGATHA_ORACLE_PASSWORD": ro_pw,
                "AGATHA_ORACLE_CDB_DSN": AGATHA_CDB_DSN, "AGATHA_ORACLE_CDB_USER": CDB_USER,
                "AGATHA_ORACLE_CDB_PASSWORD": cdb_pw})
    print(f"conexões do {RO_USER} e do {CDB_USER} gravadas em {ENV_FILE}")


def setup_cdb_user(system_pw: str) -> str:
    """Usuário comum C##AGATHA_RO no contêiner raiz: só CREATE SESSION e SELECT em CDB_VIEWS."""
    pw = secret_file("c_agatha_ro-password")
    with oracledb.connect(user="sys", password=system_pw, dsn=CDB_DSN, mode=oracledb.AUTH_MODE_SYSDBA) as conn:
        cur = conn.cursor()
        if exists(cur, "SELECT COUNT(*) FROM dba_users WHERE username = :u", u=CDB_USER):
            cur.execute(f'ALTER USER {CDB_USER} IDENTIFIED BY "{pw}" CONTAINER=ALL')
            print(f"usuário {CDB_USER}: já existia")
        else:
            cur.execute(f'CREATE USER {CDB_USER} IDENTIFIED BY "{pw}" CONTAINER=ALL')
            print(f"usuário {CDB_USER}: criado (comum, no CDB)")
        cur.execute(f"GRANT CREATE SESSION TO {CDB_USER}")  # só no contêiner raiz
        for view in CDB_VIEWS:
            cur.execute(f"GRANT SELECT ON {view} TO {CDB_USER}")
        print(f"  privilégios no CDB: CREATE SESSION + SELECT em {', '.join(v.split('.')[1] for v in CDB_VIEWS)}")
    with oracledb.connect(user=CDB_USER, password=pw, dsn=CDB_DSN) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM v$rman_backup_job_details")
        print(f"{CDB_USER} lê os backups: {cur.fetchone()[0]} jobs do RMAN")
        try:
            cur.execute("CREATE TABLE teste_escrita (x NUMBER)")
            sys.exit(f"ATENÇÃO: {CDB_USER} conseguiu criar uma tabela")
        except oracledb.DatabaseError as e:
            print(f"{CDB_USER} não cria nada ({str(e).splitlines()[0]})")
    return pw


if __name__ == "__main__":
    main()
