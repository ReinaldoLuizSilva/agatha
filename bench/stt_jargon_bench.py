"""Benchmark do STT no jargão do Reinaldo (Fase 6, ADR-018).

Roda o pipeline do servidor (Silero VAD + faster-whisper `small` int8, os mesmos parâmetros do
`Transcriber`) no CPU, fora da Agatha: não disputa a GPU e não passa pelo LLM (o roteiro tem
"guarda que o PRDORA...", que pela Agatha real gravaria um fato na memória dele). As gravações são
as da parte `jargao` do gravador (`python -m agatha_client.record --part jargao`), com a voz dele.

Mede, para cada variante:
- WER (taxa de erro de palavras), sem acento, pontuação e com números por extenso;
- acerto dos termos de jargão presentes em cada frase (RMAN, Data Guard, PRDORA...);
- tempo de transcrição (no CPU: só para comparar variantes entre si).

Uso (com teto de memória, como todo teste mais pesado):
    cap="systemd-run --user --scope -p MemoryMax=3G -p MemorySwapMax=0 nice -n 19"
    $cap .venv/bin/python bench/stt_jargon_bench.py                       # vocabulário atual do servidor
    $cap .venv/bin/python bench/stt_jargon_bench.py --label sem-vocab --prompt ""
    $cap .venv/bin/python bench/stt_jargon_bench.py --label hotwords --hotwords
    $cap .venv/bin/python bench/stt_jargon_bench.py --dir /dados/agatha/data/wakeword/gravacoes --part perto
    .venv/bin/python bench/stt_jargon_bench.py --rescore bench/results/stt-jargao-*.json   # critérios atuais
"""

import argparse
import json
import re
import statistics
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from num2words import num2words

from agatha_server.config import load_config
from agatha_server.stt import STTConfig, Transcriber
from agatha_server.vad import Segmenter, SileroVAD, SpeechEnd, VADConfig

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "bench" / "results"
DEFAULT_DIR = Path("/dados/agatha/data/stt/gravacoes-stt")
CHUNK = 512

# Termos cobrados quando aparecem na frase de referência (comparados já normalizados)
TERMS = ["agatha", "rman", "awr", "ash", "asm", "data guard", "cdb", "pdb", "spfile", "undo", "redo log",
         "archivelog", "listener", "flashback", "prdora", "agatha srv", "zabbix", "item", "trigger", "triggers",
         "template", "housekeeping", "proxy", "grafana", "postgresql", "docker", "podman", "kubernetes",
         "ansible", "terraform", "cpu", "oracle", "tablespace", "lock", "backup", "incremental", "diferencial",
         "valida", "verifica", "terabytes", "gigabytes", "severidade"]


def normalize(text: str) -> str:
    text = re.sub(r"\d+", lambda m: f" {num2words(int(m.group()), lang='pt_BR')} ", text.lower().replace("%", " por cento"))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """(erros, palavras da referência): distância de edição em palavras."""
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)], len(r)


def has_term(term: str, text: str) -> bool:
    """O termo, aceitando espaço no meio ("data guard" = "dataguard", "sp file") e plural."""
    stem = term[:-1] if term.endswith("s") else term
    pattern = r"\s?".join(re.escape(c) for c in stem.replace(" ", ""))
    return re.search(rf"\b{pattern}s?\b", text) is not None


def score(rows: list[dict]) -> tuple[str, dict]:
    errors = words = hits = total = 0
    misses: dict[str, int] = {}
    for r in rows:
        ref, hyp = normalize(r["ref"]), normalize(r["hyp"])
        e, n = wer(ref, hyp)
        terms = [t for t in TERMS if has_term(t, ref)]
        missed = [t for t in terms if not has_term(t, hyp)]
        r.update(errors=e, words=n, terms=terms, missed=missed)
        errors, words, hits, total = errors + e, words + n, hits + len(terms) - len(missed), total + len(terms)
        for t in missed:
            misses[t] = misses.get(t, 0) + 1
    line = (f"WER {100 * errors / max(words, 1):.1f}% ({errors}/{words} palavras) · "
            f"jargão {hits}/{total} ({100 * hits / max(total, 1):.0f}%) · "
            f"frases perfeitas {sum(not r['errors'] for r in rows)}/{len(rows)}")
    return line, misses


def load_items(folder: Path, parts: list[str] | None) -> list[dict]:
    entries = {}
    for line in (folder / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        entries[e["id"]] = e  # vale a última linha de cada id
    return [e for e in entries.values() if e.get("text") and (not parts or e["part"] in parts)]


def read_audio(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help=f"pasta das gravações (padrão: {DEFAULT_DIR})")
    parser.add_argument("--part", nargs="+", help="só estas partes do manifest")
    parser.add_argument("--label", default="atual")
    parser.add_argument("--prompt", help="troca o vocabulário (initial_prompt); \"\" para nenhum")
    parser.add_argument("--hotwords", action="store_true", help="passa o vocabulário como hotwords, não initial_prompt")
    parser.add_argument("--beam", type=int, help="beam_size (padrão: o do servidor)")
    parser.add_argument("--gain", type=float, metavar="DBFS",
                        help="normaliza o pico de cada frase para este nível antes do STT (ex.: -3)")
    parser.add_argument("--rescore", nargs="+", metavar="JSON", help="re-pontua resultados salvos e sai")
    args = parser.parse_args()
    if args.rescore:
        for path in args.rescore:
            data = json.loads(Path(path).read_text())
            line, misses = score(data["rows"])
            print(f"{data['label']:16s} {line} · perdidos: {', '.join(f'{t}×{n}' for t, n in sorted(misses.items(), key=lambda x: -x[1]))}")
        return

    server = load_config({"AGATHA_ENV_FILE": str(ROOT / "server" / ".env")}).stt
    config = STTConfig(device="cpu", cpu_threads=3, names=server.names,
                       initial_prompt=server.initial_prompt if args.prompt is None else (args.prompt or None),
                       beam_size=args.beam or server.beam_size)
    stt = Transcriber(config)
    vocabulary = config.vocabulary()
    vad = SileroVAD()

    def transcribe(audio: np.ndarray) -> str:
        segments, _ = stt._model.transcribe(
            audio, language=config.language, beam_size=config.beam_size, best_of=config.best_of,
            temperature=list(config.temperatures), vad_filter=False, without_timestamps=True,
            condition_on_previous_text=False,
            **({"hotwords": vocabulary} if args.hotwords else {"initial_prompt": vocabulary}))
        return "".join(s.text for s in segments).strip()

    items = load_items(args.dir, args.part)
    print(f"{len(items)} gravações · vocabulário ({'hotwords' if args.hotwords else 'initial_prompt'}): "
          f"{vocabulary} · beam {config.beam_size}\n")
    rows, errors, words, hits, total_terms, times = [], 0, 0, 0, 0, []
    for item in items:
        audio = np.concatenate([read_audio(args.dir / item["file"]), np.zeros(16000, np.float32)])
        vad.reset()
        segmenter = Segmenter(VADConfig(), vad)
        texts = []
        for start in range(0, len(audio), CHUNK):
            for ev in segmenter.feed(audio[start : start + CHUNK]):
                if isinstance(ev, SpeechEnd):
                    segment = ev.audio
                    if args.gain is not None and (peak := float(np.max(np.abs(segment)))) > 0:
                        segment = segment * min(10 ** (args.gain / 20) / peak, 30.0)  # no máximo +30 dB
                    t0 = time.perf_counter()
                    texts.append(transcribe(segment))
                    times.append(time.perf_counter() - t0)
        hyp_raw = " ".join(texts)
        ref, hyp = normalize(item["text"]), normalize(hyp_raw)
        e, n = wer(ref, hyp)
        terms = [t for t in TERMS if has_term(t, ref)]
        missed = [t for t in terms if not has_term(t, hyp)]
        errors, words = errors + e, words + n
        hits, total_terms = hits + len(terms) - len(missed), total_terms + len(terms)
        rows.append({"id": item["id"], "ref": item["text"], "hyp": hyp_raw, "errors": e, "words": n,
                     "terms": terms, "missed": missed, "segments": len(texts)})
        mark = "✅" if not e else ("🟡" if not missed else "❌")
        print(f"{mark} {item['id']:12s} {hyp_raw[:80]!r}" + (f"  · perdeu {missed}" if missed else ""), flush=True)

    misses: dict[str, int] = {}
    for r in rows:
        for t in r["missed"]:
            misses[t] = misses.get(t, 0) + 1
    print(f"\nWER {100 * errors / max(words, 1):.1f}% ({errors}/{words} palavras) · "
          f"jargão {hits}/{total_terms} ({100 * hits / max(total_terms, 1):.0f}%) · "
          f"frases perfeitas {sum(not r['errors'] for r in rows)}/{len(rows)} · "
          f"STT p50 {statistics.median(times):.2f} s (CPU)")
    if misses:
        print("termos perdidos:", ", ".join(f"{t} ×{n}" for t, n in sorted(misses.items(), key=lambda x: -x[1])))

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"stt-jargao-{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "label": args.label,
                               "vocabulary": vocabulary, "hotwords": args.hotwords, "beam": config.beam_size,
                               "gain": args.gain,
                               "wer": errors / max(words, 1), "terms": [hits, total_terms], "rows": rows},
                              ensure_ascii=False, indent=2))
    print(f"resultados: {out}")


if __name__ == "__main__":
    main()
