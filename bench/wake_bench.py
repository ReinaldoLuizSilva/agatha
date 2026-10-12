"""Benchmark de detectores da wake word "Agatha" (Fase 5, etapa 2).

Roda cada detector sobre as gravações do Reinaldo (python -m agatha_client.record) e sobre fala
PT-BR sem o nome (FLEURS) e mede:

- acerto (recall) nas falas com o nome: perto, variado, longe e eco (interrupção com a Agatha
  falando pelas caixas, ainda SEM cancelamento de eco);
- falsos disparos nas falas sem o nome: negativas difíceis ("a gata", "agora"), eco calado (só a
  voz da Agatha), ambiente (TV/podcast) e FLEURS, também em disparos por hora.

Detectores:
    stt          o que roda hoje (ADR-008): Silero VAD + Segmenter + Whisper small (CPU, mesmo
                 vocabulário do servidor) + wake.py. Precisa do .venv do servidor.
    vosk         Vosk PT-BR pequeno com gramática restrita ao nome (+ [unk]); dispara se a palavra
                 sair com confiança >= limiar (varre vários limiares).
    vosk-livre   Vosk sem gramática (vocabulário completo); dispara se "agatha" aparecer.

Uso:
    .venv/bin/python bench/wake_bench.py stt
    .venv-wake/bin/python bench/wake_bench.py vosk vosk-livre      # pip install -r bench/requirements-wake.txt
    .venv-wake/bin/python bench/wake_bench.py vosk --no-fleurs     # rápido, sem o FLEURS
"""

import argparse
import json
import os
import platform
import sys
import time
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "bench" / "results"
DATA = Path("/dados/agatha/data/wakeword/gravacoes")
FLEURS = Path("/dados/agatha/data/fleurs/pt_br/dev")
VOSK_MODEL = "/dados/agatha/models/vosk/vosk-model-small-pt-0.3"
SAMPLE_RATE = 16000
CHUNK = 512  # o bloco que o cliente envia (32 ms)
NAME = "agatha"
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99)

POSITIVE = {"perto", "variado", "longe", "eco-fala"}
NEGATIVE = {"negativas", "eco-calado", "ambiente", "fleurs"}


# --- Dados -----------------------------------------------------------------------------

def read_audio(path: Path) -> np.ndarray:
    """float32 mono 16 kHz (WAV PCM 16 bits com o wave; float com o soundfile)."""
    try:
        with wave.open(str(path), "rb") as w:
            assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    except wave.Error:
        import soundfile as sf

        audio, rate = sf.read(str(path), dtype="float32")
        assert rate == SAMPLE_RATE and audio.ndim == 1, path
        return audio


def load_items(fleurs: bool) -> list[dict]:
    """Cada item: id, grupo, texto esperado e o caminho do áudio (16 kHz mono)."""
    rows = {}
    for line in (DATA / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row  # vale a última linha (regravações)
    group_of = {"wake": None, "no_wake": "negativas", "echo_wake": "eco-fala", "echo_only": "eco-calado",
                "ambient": "ambiente"}
    items = [{"id": r["id"], "group": group_of[r["label"]] or r["part"], "text": r["text"],
              "path": DATA / r["file"]} for r in rows.values()]
    if fleurs:
        items += [{"id": f"fleurs-{p.stem}", "group": "fleurs", "text": None, "path": p}
                  for p in sorted(FLEURS.glob("*.wav"))]
    return items


# --- Detectores ------------------------------------------------------------------------

class SttDetector:
    """O pipeline do servidor, no CPU: cada frase que o VAD fecha vira um evento com o texto."""

    name = "stt"

    def __init__(self):
        sys.path.insert(0, str(ROOT / "server"))
        from agatha_server.config import load_config
        from agatha_server.stt import STTConfig, Transcriber
        from agatha_server.vad import Segmenter, SileroVAD, SpeechEnd, VADConfig
        from agatha_server.wake import WakeConfig, is_addressed

        names = load_config({"AGATHA_ENV_FILE": str(ROOT / "server" / ".env")}).stt.names
        self._config = STTConfig(device="cpu", cpu_threads=3, names=names)
        self._transcriber = Transcriber(self._config)
        self._vad = SileroVAD()
        self._make_segmenter = lambda: Segmenter(VADConfig(), self._vad)
        self._speech_end = SpeechEnd
        self._is_addressed = is_addressed
        self._wake = WakeConfig()
        self.description = f"Whisper small CPU int8, vocabulário: {self._config.vocabulary()}"

    def detect(self, audio: np.ndarray) -> list[dict]:
        self._vad.reset()
        segmenter = self._make_segmenter()
        tail = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1 s de silêncio para fechar a última frase
        events = []
        for start in range(0, len(audio) + len(tail), CHUNK):
            chunk = np.concatenate([audio, tail])[start : start + CHUNK]
            for ev in segmenter.feed(chunk):
                if isinstance(ev, self._speech_end):
                    text = self._transcriber.transcribe(ev.audio)
                    events.append({"text": text, "end_s": ev.speech_end_ms / 1000,
                                   "hit": self._is_addressed(text, self._wake)})
        return events

    @staticmethod
    def fires(events: list[dict], threshold: float | None = None) -> int:
        return sum(e["hit"] for e in events)


class VoskDetector:
    """Vosk em streaming, blocos de 32 ms como o cliente; guarda as palavras com confiança."""

    def __init__(self, grammar: bool):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        self._model = Model(VOSK_MODEL)
        self._grammar = json.dumps([NAME, "[unk]"]) if grammar else None
        self._make = KaldiRecognizer
        self.name = "vosk" if grammar else "vosk-livre"
        self.description = f"{Path(VOSK_MODEL).name}, " + (f"gramática {self._grammar}" if grammar else "sem gramática")

    def detect(self, audio: np.ndarray) -> list[dict]:
        rec = (self._make(self._model, SAMPLE_RATE, self._grammar) if self._grammar
               else self._make(self._model, SAMPLE_RATE))
        rec.SetWords(True)
        pcm = np.clip(np.round(audio * 32768), -32768, 32767).astype("<i2").tobytes()
        events, first_partial = [], None
        for i, start in enumerate(range(0, len(pcm), 2 * CHUNK)):
            if rec.AcceptWaveform(pcm[start : start + 2 * CHUNK]):
                events += self._words(json.loads(rec.Result()), first_partial)
                first_partial = None
            elif first_partial is None and NAME in json.loads(rec.PartialResult()).get("partial", "").split():
                first_partial = (i + 1) * CHUNK / SAMPLE_RATE  # quando o nome apareceu no parcial
        events += self._words(json.loads(rec.FinalResult()), first_partial)
        return events

    @staticmethod
    def _words(result: dict, first_partial: float | None) -> list[dict]:
        return [{"word": w["word"], "conf": round(w["conf"], 3), "start_s": w["start"], "end_s": w["end"],
                 "partial_s": first_partial, "text": result.get("text", "")}
                for w in result.get("result", []) if w["word"] == NAME]

    @staticmethod
    def fires(events: list[dict], threshold: float | None = None) -> int:
        return sum(e["conf"] >= (threshold or 0) for e in events)


# --- Avaliação -------------------------------------------------------------------------

def run(detector, items: list[dict]) -> dict:
    results, cpu0, wall0 = [], time.process_time(), time.perf_counter()
    audio_s = 0.0
    for n, item in enumerate(items, 1):
        audio = read_audio(item["path"])
        audio_s += len(audio) / SAMPLE_RATE
        events = detector.detect(audio)
        results.append({"id": item["id"], "group": item["group"], "text": item["text"],
                        "duration_s": round(len(audio) / SAMPLE_RATE, 2), "events": events})
        if n % 25 == 0 or n == len(items):
            print(f"  {detector.name}: {n}/{len(items)}", file=sys.stderr, flush=True)
    return {"detector": detector.name, "description": detector.description, "items": results,
            "audio_s": round(audio_s, 1), "cpu_s": round(time.process_time() - cpu0, 1),
            "wall_s": round(time.perf_counter() - wall0, 1)}


def score(run_result: dict, fires, threshold: float | None) -> dict:
    groups: dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0, "fires": 0, "hours": 0.0})
    for item in run_result["items"]:
        g = groups[item["group"]]
        n_fires = fires(item["events"], threshold)
        g["n"] += 1
        g["hits"] += n_fires > 0
        g["fires"] += n_fires
        g["hours"] += item["duration_s"] / 3600
    out = {}
    for name, g in groups.items():
        if name in POSITIVE:
            out[name] = {"recall": round(g["hits"] / g["n"], 3), "n": g["n"]}
        else:
            out[name] = {"false_fires": g["fires"], "items_fired": g["hits"], "n": g["n"],
                         "hours": round(g["hours"], 3), "per_hour": round(g["fires"] / g["hours"], 1)}
    pos = [i for i in run_result["items"] if i["group"] in POSITIVE - {"eco-fala"}]
    out["recall_sem_eco"] = round(sum(fires(i["events"], threshold) > 0 for i in pos) / len(pos), 3)
    neg_h = sum(g["hours"] for k, g in groups.items() if k in NEGATIVE - {"negativas"})
    neg_f = sum(g["fires"] for k, g in groups.items() if k in NEGATIVE - {"negativas"})
    out["disparos_por_hora_fala_comum"] = round(neg_f / neg_h, 1) if neg_h else None
    return out


def table(name: str, scores: dict[str, dict]) -> str:
    cols = ["perto", "variado", "longe", "eco-fala"]
    negs = ["negativas", "eco-calado", "ambiente", "fleurs"]
    lines = [f"### {name}", "",
             "| limiar | recall (sem eco) | " + " | ".join(cols) + " | " + " | ".join(negs) + " | disparos/h (eco calado + ambiente + FLEURS) |",
             "|---" * (3 + len(cols) + len(negs)) + "|"]
    for thr, s in scores.items():
        row = [thr, f"{s['recall_sem_eco']:.0%}"]
        row += [f"{s[c]['recall']:.0%}" if c in s else "-" for c in cols]
        row += [f"{s[c]['false_fires']}/{s[c]['n']}" if c in s else "-" for c in negs]
        row.append(str(s["disparos_por_hora_fala_comum"]))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("detectors", nargs="+", choices=["stt", "vosk", "vosk-livre"])
    parser.add_argument("--no-fleurs", action="store_true", help="pula o FLEURS (1,3 h de fala)")
    args = parser.parse_args()

    items = load_items(fleurs=not args.no_fleurs)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in args.detectors:
        detector = SttDetector() if name == "stt" else VoskDetector(grammar=name == "vosk")
        result = run(detector, items)
        thresholds = THRESHOLDS if name == "vosk" else (None,)
        result["scores"] = {str(t or "-"): score(result, detector.fires, t) for t in thresholds}
        result["host"] = {"cpu": platform.processor() or platform.machine(), "threads": os.cpu_count()}
        out = RESULTS_DIR / f"wake-{name}-{stamp}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(table(f"{name}: {result['description']}", result["scores"]))
        print(f"\nCPU: {result['cpu_s']} s para {result['audio_s']} s de áudio "
              f"({result['cpu_s'] / result['audio_s']:.3f} s de CPU por s de áudio) → {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
