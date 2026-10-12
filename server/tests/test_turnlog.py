import json
from datetime import datetime

from agatha_server.turnlog import TurnLog


def test_um_arquivo_por_mes_em_json_lines(tmp_path):
    now = datetime(2026, 10, 12, 13, 30, 5, 123000)
    log = TurnLog(tmp_path / "logs", now=lambda: now)
    log.write("transcript", session="ab12", text="Que horas são?", outcome="response")
    log.write("response", session="ab12", text="São 13h30.", e2e_ms=1900)
    lines = (tmp_path / "logs" / "turns-2026-10.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"ts": "2026-10-12T13:30:05.123", "event": "transcript", "session": "ab12", "text": "Que horas são?",
         "outcome": "response"},
        {"ts": "2026-10-12T13:30:05.123", "event": "response", "session": "ab12", "text": "São 13h30.",
         "e2e_ms": 1900},
    ]


def test_falha_de_disco_nao_quebra(tmp_path):
    blocked = tmp_path / "arquivo"
    blocked.write_text("")  # um arquivo no lugar do diretório
    TurnLog(blocked).write("response", text="x")  # só registra um aviso
