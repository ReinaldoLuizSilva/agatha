"""Registro de cada fala e de cada resposta em JSON Lines, para medir a Agatha ao longo do tempo.

Um arquivo por mês (`turns-2026-10.jsonl`) em AGATHA_LOG_DIR. Eventos:
- `connect`: nova sessão, com o nome do cliente (ex.: `agatha-client/0.3.0 (win32)`, `send_wav`) e o IP;
- `transcript`: toda transcrição, com o que aconteceu com ela (`outcome`): virou resposta, foi
  ignorada (sem o nome, resposta em andamento), ficou esperando continuação ou veio vazia;
- `response`: cada resposta, com o texto, as ferramentas, os tempos de cada etapa e o `e2e_ms`
  medido no cliente (fim da fala → primeiro som), que chega no `playback_end`.

Resumo: python bench/turns_report.py /dados/agatha/logs/turns-*.jsonl
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class TurnLog:
    def __init__(self, directory: str | Path, now: Callable[[], datetime] = datetime.now):
        self.directory = Path(directory)
        self._now = now

    def write(self, event: str, **fields) -> None:
        """Acrescenta um evento. Falha de disco vira aviso: o registro nunca derruba a conversa."""
        now = self._now()
        record = {"ts": now.isoformat(timespec="milliseconds"), "event": event, **fields}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.directory / f"turns-{now:%Y-%m}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("falha ao gravar o registro de turnos: %s", e)
