"""Resumo do registro de turnos da Agatha (AGATHA_LOG_DIR/turns-AAAA-MM.jsonl).

Mostra, por sessão e no total, a latência do fim da fala ao primeiro som (medida no cliente),
quantas falas foram ignoradas e por quê, as ferramentas usadas e as respostas mais lentas.

Uso:
    python bench/turns_report.py /dados/agatha/logs/turns-*.jsonl
    python bench/turns_report.py --since 2026-10-12 /dados/agatha/logs/turns-*.jsonl
    python bench/turns_report.py --client agatha-client /dados/agatha/logs/turns-*.jsonl  # sem os testes
"""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def ms(value: float | None) -> str:
    return "-" if value is None else f"{value / 1000:.2f} s"


def load(paths: list[Path], since: str | None) -> list[dict]:
    events = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if since is None or event["ts"] >= since:
                    events.append(event)
    return sorted(events, key=lambda e: e["ts"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--since", help="só eventos a partir desta data/hora (AAAA-MM-DD[THH:MM])")
    parser.add_argument("--client", help="só sessões cujo cliente contém este texto (ex.: agatha-client)")
    parser.add_argument("--slowest", type=int, default=5)
    args = parser.parse_args()

    events = load(args.files, args.since)
    clients = {e["session"]: e["client"] for e in events if e["event"] == "connect"}
    if args.client:
        events = [e for e in events if args.client in clients.get(e["session"], "")]
    responses = [e for e in events if e["event"] == "response"]
    transcripts = [e for e in events if e["event"] == "transcript"]
    if not responses:
        print("nenhuma resposta registrada")
        return

    sessions: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        sessions[e["session"]].append(e)

    print("| sessão | cliente | início | respostas | 1º som p50 | p90 | máx | ignoradas |")
    print("|---|---|---|---|---|---|---|---|")
    for sid, evs in sessions.items():
        e2e = [e["e2e_ms"] for e in evs if e["event"] == "response" and e.get("e2e_ms") is not None]
        n = sum(e["event"] == "response" for e in evs)
        ignored = Counter(e["outcome"] for e in evs if e["event"] == "transcript"
                          and e["outcome"] in ("not_addressed", "busy"))
        ign = ", ".join(f"{k} {v}" for k, v in ignored.items()) or "-"
        print(f"| {sid} | {clients.get(sid, '?')} | {evs[0]['ts'][:16].replace('T', ' ')} | {n} | {ms(pct(e2e, .5))} | {ms(pct(e2e, .9))} "
              f"| {ms(max(e2e) if e2e else None)} | {ign} |")

    e2e = [r["e2e_ms"] for r in responses if r.get("e2e_ms") is not None]
    print(f"\nTotal: {len(responses)} respostas em {len(sessions)} sessões")
    print("\n| etapa (a partir do fim da fala) | p50 | p90 | máx |")
    print("|---|---|---|---|")
    for key, label in (("stt_ms", "transcrição pronta"), ("llm_first_token_ms", "1º token do LLM"),
                       ("first_audio_ms", "1º áudio (servidor)")):
        values = [r["timings"][key] for r in responses if r["timings"].get(key) is not None]
        print(f"| {label} | {ms(pct(values, .5))} | {ms(pct(values, .9))} | {ms(max(values) if values else None)} |")
    print(f"| 1º som (cliente) | {ms(pct(e2e, .5))} | {ms(pct(e2e, .9))} | {ms(max(e2e) if e2e else None)} |")
    if e2e:
        print(f"\nDentro da meta de 2 s: {sum(v < 2000 for v in e2e)}/{len(e2e)}"
              f" · média {statistics.mean(e2e) / 1000:.2f} s")

    outcomes = Counter(t["outcome"] for t in transcripts)
    print("\nTranscrições: " + ", ".join(f"{k} {v}" for k, v in outcomes.most_common()))
    tools = Counter(tool for r in responses for tool in r.get("tools", []))
    print("Ferramentas: " + (", ".join(f"{k} {v}" for k, v in tools.most_common()) or "nenhuma"))
    errors = [r for r in responses if r.get("reason") != "done"]
    if errors:
        print(f"Respostas com erro: {len(errors)}")

    print(f"\nMais lentas (1º som no cliente):")
    for r in sorted((r for r in responses if r.get("e2e_ms") is not None), key=lambda r: -r["e2e_ms"])[:args.slowest]:
        tools = f" [{', '.join(r['tools'])}]" if r.get("tools") else ""
        print(f"  {ms(r['e2e_ms'])}  {r['ts'][:19].replace('T', ' ')}  {r['user_text'][:50]!r}{tools}")


if __name__ == "__main__":
    main()
