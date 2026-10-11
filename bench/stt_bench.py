"""Benchmark de STT (faster-whisper) em PT-BR na CPU.

Mede, para cada modelo:
- tempo de carga do modelo;
- latência de transcrição e RTF em frases completas do FLEURS (pt_br);
- WER contra a transcrição de referência (texto normalizado);
- latência em recortes curtos (padrão 3 s), simulando um comando de voz;
- pico de RAM do processo.

Cada modelo roda num processo próprio, para que RAM e tempo de carga não se misturem.

Uso:
    .venv/bin/python bench/stt_bench.py --models tiny,base,small
"""

import argparse
import csv
import json
import platform
import random
import re
import resource
import statistics
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from multiprocessing import get_context
from pathlib import Path

SAMPLE_RATE = 16000
MAX_SECONDS = 30  # acima disso o Whisper usa duas janelas e distorce a latência
DEFAULT_DATASET = Path("/dados/agatha/data/fleurs/pt_br")
DEFAULT_MODELS_DIR = Path("/dados/agatha/models/whisper")
RESULTS_DIR = Path(__file__).parent / "results"


# --- Normalização de texto para o WER ---------------------------------------

def _number_to_words(match: re.Match) -> str:
    from num2words import num2words

    token = match.group(0)
    try:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", token):  # 1.575 (milhar)
            return num2words(int(token.replace(".", "")), lang="pt_BR")
        if re.fullmatch(r"\d+,\d+", token):  # 2,5 (decimal)
            whole, frac = token.split(",")
            return f"{num2words(int(whole), lang='pt_BR')} vírgula {num2words(int(frac), lang='pt_BR')}"
        return num2words(int(re.sub(r"\D", "", token)), lang="pt_BR")
    except (ValueError, OverflowError):
        return token


def normalize(text: str) -> str:
    """Minúsculas, números por extenso, sem pontuação; mantém acentos."""
    text = text.lower().replace("%", " por cento")
    text = re.sub(r"\d+(?:[.,]\d+)*", _number_to_words, text)
    text = "".join(" " if unicodedata.category(c).startswith(("P", "S")) else c for c in text)
    return " ".join(text.split())


# --- Seleção de amostras ------------------------------------------------------

def load_samples(dataset_dir: Path, n: int, seed: int) -> list[dict]:
    """Escolhe n frases distintas do split dev (uma gravação por frase, até 30 s)."""
    by_sentence: dict[str, dict] = {}
    with open(dataset_dir / "dev.tsv", newline="", encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            sentence_id, filename, raw_text, num_samples = row[0], row[1], row[2], int(row[5])
            if num_samples > MAX_SECONDS * SAMPLE_RATE:
                continue
            by_sentence.setdefault(sentence_id, {"file": filename, "reference": raw_text})
    rng = random.Random(seed)
    chosen = rng.sample(sorted(by_sentence.values(), key=lambda s: s["file"]), n)
    for s in chosen:
        s["path"] = str(dataset_dir / "dev" / s["file"])
    return chosen


# --- Worker (um processo por modelo) -----------------------------------------

def _transcribe(model, audio, beam_size: int) -> str:
    segments, _ = model.transcribe(
        audio,
        language="pt",
        beam_size=beam_size,
        vad_filter=False,  # o VAD fica no pipeline, não aqui
        without_timestamps=True,
        condition_on_previous_text=False,
    )
    return "".join(s.text for s in segments).strip()  # o gerador só decodifica ao ser consumido


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "mean": statistics.fmean(ordered),
        "p50": statistics.median(ordered),
        "p90": ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))],
        "max": ordered[-1],
    }


def run_model(cfg: dict) -> dict:
    import jiwer
    import soundfile as sf
    from faster_whisper import WhisperModel

    # FLEURS já vem em 16 kHz mono (float32), o formato que o Whisper espera
    audios = [sf.read(s["path"], dtype="float32")[0] for s in cfg["samples"]]

    t0 = time.perf_counter()
    model = WhisperModel(
        cfg["model"],
        device="cpu",
        compute_type=cfg["compute_type"],
        cpu_threads=cfg["threads"],
        download_root=cfg["models_dir"],
    )
    load_s = time.perf_counter() - t0

    _transcribe(model, audios[0], cfg["beam_size"])  # aquecimento, fora da medição

    full = []
    for sample, audio in zip(cfg["samples"], audios):
        t0 = time.perf_counter()
        text = _transcribe(model, audio, cfg["beam_size"])
        elapsed = time.perf_counter() - t0
        duration = len(audio) / SAMPLE_RATE
        full.append({
            "file": sample["file"],
            "duration_s": duration,
            "latency_s": elapsed,
            "rtf": elapsed / duration,
            "reference": sample["reference"],
            "hypothesis": text,
        })

    short_len = int(cfg["short_seconds"] * SAMPLE_RATE)
    short_latencies = []
    for audio in audios:
        t0 = time.perf_counter()
        _transcribe(model, audio[:short_len], cfg["beam_size"])
        short_latencies.append(time.perf_counter() - t0)

    wer = jiwer.wer(
        [normalize(r["reference"]) for r in full],
        [normalize(r["hypothesis"]) for r in full],
    )
    return {
        "model": cfg["model"],
        "compute_type": cfg["compute_type"],
        "beam_size": cfg["beam_size"],
        "load_s": load_s,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "wer": wer,
        "full_latency_s": _stats([r["latency_s"] for r in full]),
        "full_rtf": _stats([r["rtf"] for r in full]),
        "short_latency_s": _stats(short_latencies),
        "samples": full,
    }


# --- Main ---------------------------------------------------------------------

def _cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default="tiny,base,small,medium,large-v3-turbo")
    parser.add_argument("--n", type=int, default=30, help="número de frases do FLEURS")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--short-seconds", type=float, default=3.0)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    args = parser.parse_args()

    import faster_whisper
    import ctranslate2

    samples = load_samples(args.dataset_dir, args.n, args.seed)
    audio_total = sum(Path(s["path"]).stat().st_size for s in samples)
    print(f"{len(samples)} frases do FLEURS pt_br dev ({audio_total / 1e6:.1f} MB de WAV)\n")

    results = []
    ctx = get_context("spawn")
    for name in args.models.split(","):
        cfg = {
            "model": name.strip(),
            "compute_type": args.compute_type,
            "beam_size": args.beam_size,
            "threads": args.threads,
            "short_seconds": args.short_seconds,
            "models_dir": str(args.models_dir),
            "samples": samples,
        }
        print(f"→ {cfg['model']} ...", flush=True)
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            r = pool.submit(run_model, cfg).result()
        print(
            f"  WER {r['wer']:.1%} | frase completa p50 {r['full_latency_s']['p50']:.2f}s "
            f"(RTF {r['full_rtf']['p50']:.2f}) | comando {args.short_seconds:.0f}s p50 "
            f"{r['short_latency_s']['p50']:.2f}s | RAM {r['peak_rss_mb']:.0f} MB",
            flush=True,
        )
        results.append(r)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"stt-{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "host": platform.node(),
        "cpu": _cpu_model(),
        "faster_whisper": faster_whisper.__version__,
        "ctranslate2": ctranslate2.__version__,
        "args": {k: str(v) for k, v in vars(args).items()},
        "results": results,
    }, ensure_ascii=False, indent=2))

    print(f"\n| modelo | WER | frase p50 (s) | RTF p50 | comando {args.short_seconds:.0f}s p50 (s) | p90 (s) | carga (s) | RAM (MB) |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['model']} | {r['wer']:.1%} | {r['full_latency_s']['p50']:.2f} | {r['full_rtf']['p50']:.2f} "
            f"| {r['short_latency_s']['p50']:.2f} | {r['short_latency_s']['p90']:.2f} "
            f"| {r['load_s']:.1f} | {r['peak_rss_mb']:.0f} |"
        )
    print(f"\nResultados completos: {out}")


if __name__ == "__main__":
    main()
