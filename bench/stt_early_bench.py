"""Benchmark da transcrição antecipada (Fase 11, item 1): começar o STT antes de confirmar o fim da fala.

Hoje a frase só fecha depois de `min_silence_ms` (500 ms) de silêncio, e só então o Whisper roda
(~360–440 ms na GPU, quase igual para qualquer tamanho de fala: o encoder sempre processa 30 s).
A ideia: transcrever já aos K ms de silêncio; se o silêncio chegar aos 500 ms, o texto está pronto
mais cedo; se ele voltar a falar, a transcrição é descartada.

Para cada K, nas gravações da voz dele, mede:
- se o texto antecipado (áudio até K ms de silêncio) sai igual ao texto normal (até 500 ms), e o WER
  e o jargão dos dois contra a referência;
- quantas transcrições seriam jogadas fora (pausas de K a 500 ms no meio da fala), por frase.

Roda no CPU (os textos são os mesmos da GPU; os tempos da GPU vêm do registro de turnos), com teto
de memória, sem parar a Agatha:
    cap="systemd-run --user --scope -p MemoryMax=3G -p MemorySwapMax=0 nice -n 19"
    PYTHONPATH=server:bench $cap .venv/bin/python bench/stt_early_bench.py
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

import numpy as np

from agatha_server.config import load_config
from agatha_server.stt import STTConfig, Transcriber
from agatha_server.vad import FRAME_MS, FRAME_SAMPLES, Segmenter, SileroVAD, SpeechEnd
from stt_jargon_bench import CHUNK, RESULTS_DIR, ROOT, TERMS, has_term, load_items, normalize, read_audio, wer

SOURCES = [  # (pasta, partes): jargão (mic do notebook, 18/10) e comandos curtos com o headset (20/10)
    (Path("/dados/agatha/data/stt/gravacoes-stt"), ["jargao"]),
    (Path("/dados/agatha/data/wakeword/gravacoes-headset"), ["perto", "variado"]),
]
EARLY_FRAMES = [3, 5, 6, 8]  # 96, 160, 192 e 256 ms de silêncio


class RecordingVAD:
    """O Silero do servidor, guardando a probabilidade de cada bloco para a análise das pausas."""

    def __init__(self):
        self._vad = SileroVAD()
        self.probs: list[float] = []

    def __call__(self, frame: np.ndarray) -> float:
        p = self._vad(frame)
        self.probs.append(p)
        return p

    def reset(self):
        self._vad.reset()
        self.probs = []


def pauses(probs: list[float], config) -> list[int]:
    """Silêncios (em blocos) no meio da fala, com a mesma histerese do Segmenter; o final (que fecha a
    frase) não entra."""
    runs, in_speech, silence, speech = [], False, 0, 0
    min_speech = -(-config.min_speech_ms // FRAME_MS)
    limit = -(-config.min_silence_ms // FRAME_MS)
    for p in probs:
        if not in_speech:
            if p >= config.threshold:
                in_speech, silence, speech = True, 0, 1
            continue
        if p >= config.neg_threshold:
            if silence and speech >= min_speech:
                runs.append(silence)
            silence, speech = 0, speech + 1
        else:
            silence += 1
            if silence >= limit:
                in_speech = False
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="só as N primeiras gravações de cada fonte (teste rápido)")
    args = parser.parse_args()

    server = load_config({"AGATHA_ENV_FILE": str(ROOT / "server" / ".env")})
    vad_config = server.vad
    stt = Transcriber(STTConfig(device="cpu", cpu_threads=3, names=server.stt.names,
                                initial_prompt=server.stt.initial_prompt, beam_size=server.stt.beam_size))
    limit = -(-vad_config.min_silence_ms // FRAME_MS)
    vad = RecordingVAD()
    rows = []
    for folder, parts in SOURCES:
        for item in load_items(folder, parts)[: args.limit]:
            audio = np.concatenate([read_audio(folder / item["file"]), np.zeros(16000, np.float32)])
            vad.reset()
            segmenter = Segmenter(vad_config, vad)
            full, early = [], {k: [] for k in EARLY_FRAMES}
            for start in range(0, len(audio), CHUNK):
                for ev in segmenter.feed(audio[start : start + CHUNK]):
                    if isinstance(ev, SpeechEnd) and ev.reason == "silence":
                        full.append(stt.transcribe(ev.audio))
                        for k in EARLY_FRAMES:  # o mesmo áudio sem os últimos (limit - k) blocos de silêncio
                            early[k].append(stt.transcribe(ev.audio[: len(ev.audio) - (limit - k) * FRAME_SAMPLES]))
            runs = pauses(vad.probs, vad_config)
            row = {"id": item["id"], "ref": item["text"], "full": " ".join(full), "segments": len(full),
                   "pauses_ms": [r * FRAME_MS for r in runs],
                   "early": {str(k): " ".join(t) for k, t in early.items()}}
            rows.append(row)
            same = "".join("=" if normalize(row["early"][str(k)]) == normalize(row["full"]) else "≠"
                           for k in EARLY_FRAMES)
            print(f"{same} {item['id']:12s} pausas {row['pauses_ms']} {row['full'][:70]!r}", flush=True)

    print(f"\n{len(rows)} gravações, {sum(r['segments'] for r in rows)} frases")
    base = summary(rows, lambda r: r["full"])
    print(f"normal (500 ms): {base}")
    for k in EARLY_FRAMES:
        ms = k * FRAME_MS
        same = sum(normalize(r["early"][str(k)]) == normalize(r["full"]) for r in rows)
        wasted = [sum(p >= ms for p in r["pauses_ms"]) for r in rows]
        print(f"antecipado aos {ms:3d} ms: texto igual {same}/{len(rows)} · {summary(rows, lambda r: r['early'][str(k)])} · "
              f"descartadas {sum(wasted)} ({sum(wasted) / len(rows):.2f} por fala, falas com alguma "
              f"{sum(w > 0 for w in wasted)}) · ganho {(limit - k) * FRAME_MS} ms")
    all_pauses = [p for r in rows for p in r["pauses_ms"]]
    if all_pauses:
        print(f"pausas no meio da fala: {len(all_pauses)}, p50 {statistics.median(all_pauses):.0f} ms, "
              f"máx {max(all_pauses)} ms")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"stt-antecipado-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "vad": vars(vad_config),
                               "early_frames": EARLY_FRAMES, "rows": rows}, ensure_ascii=False, indent=2, default=str))
    print(f"resultados: {out}")


def summary(rows, text) -> str:
    errors = words = hits = total = 0
    for r in rows:
        ref, hyp = normalize(r["ref"]), normalize(text(r))
        e, n = wer(ref, hyp)
        terms = [t for t in TERMS if has_term(t, ref)]
        errors, words = errors + e, words + n
        hits, total = hits + sum(has_term(t, hyp) for t in terms), total + len(terms)
    return f"WER {100 * errors / max(words, 1):.1f}% · jargão {hits}/{total}"


if __name__ == "__main__":
    main()
