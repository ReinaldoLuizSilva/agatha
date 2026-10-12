"""Memória explícita da Agatha (ADR-010): fatos que o Reinaldo pede para lembrar.

Os fatos ficam num SQLite em /dados/agatha/data (fora do git), valem entre sessões e reinícios, e
entram numerados no prompt de sistema. O LLM grava com a ferramenta `guardar_fato` e apaga
com `esquecer`, pelo número.

Administração pelo terminal:
    python -m agatha_server.memory list
    python -m agatha_server.memory add "O Reinaldo usa o padrão ambiente-servidor-host no Zabbix"
    python -m agatha_server.memory forget 3
"""

import argparse
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_DB = "/dados/agatha/data/memory/agatha.db"


@dataclass(frozen=True)
class MemoryConfig:
    db: str = DEFAULT_DB
    # cada fato custa ~20-25 tokens no prompt; 30 fatos ≈ 700 tokens dos 4096 de contexto (ADR-010)
    max_facts: int = 30
    max_chars: int = 200


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    created_at: str


def _key(text: str) -> str:
    """Forma normalizada para detectar fatos repetidos (sem acento, pontuação ou caixa)."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


class MemoryStore:
    def __init__(self, config: MemoryConfig = MemoryConfig()):
        self.config = config
        if config.db != ":memory:":
            Path(config.db).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(config.db, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS facts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, key TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL)"
        )
        self._db.commit()
        self.version = 0  # muda a cada alteração: o prompt (e o cache do LLM) precisa ser refeito

    def facts(self) -> list[Fact]:
        rows = self._db.execute("SELECT id, text, created_at FROM facts ORDER BY id").fetchall()
        return [Fact(*row) for row in rows]

    def add(self, text: str) -> tuple[Fact | None, str]:
        """Grava um fato. Devolve (fato, situação): "novo", "repetido", "vazio", "longo" ou "cheio"."""
        text = " ".join(text.split()).strip()
        if not text:
            return None, "vazio"
        if len(text) > self.config.max_chars:
            return None, "longo"
        key = _key(text)
        existing = self._db.execute("SELECT id, text, created_at FROM facts WHERE key = ?", (key,)).fetchone()
        if existing:
            return Fact(*existing), "repetido"
        if len(self.facts()) >= self.config.max_facts:
            return None, "cheio"
        created = datetime.now().isoformat(timespec="seconds")
        cursor = self._db.execute("INSERT INTO facts (text, key, created_at) VALUES (?, ?, ?)", (text, key, created))
        self._db.commit()
        self.version += 1
        return Fact(cursor.lastrowid, text, created), "novo"

    def forget(self, fact_id: int) -> Fact | None:
        row = self._db.execute("SELECT id, text, created_at FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if row is None:
            return None
        self._db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self._db.commit()
        self.version += 1
        return Fact(*row)

    def close(self) -> None:
        self._db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("AGATHA_MEMORY_DB", DEFAULT_DB))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("text")
    forget = sub.add_parser("forget")
    forget.add_argument("id", type=int)
    args = parser.parse_args()

    store = MemoryStore(MemoryConfig(db=args.db))
    if args.cmd == "list":
        for f in store.facts():
            print(f"{f.id:3d}  {f.created_at}  {f.text}")
        if not store.facts():
            print("(memória vazia)")
    elif args.cmd == "add":
        fact, status = store.add(args.text)
        print(f"{status}: {fact.id if fact else '-'}")
    elif args.cmd == "forget":
        fact = store.forget(args.id)
        print(f"esquecido: {fact.text}" if fact else "não existe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
