"""Benchmark de cancelamento de eco (AEC) para o barge-in (Fase 5, etapa 3).

Usa as gravações da parte "eco" (python -m agatha_client.record): o microfone do notebook captando
a Agatha pelas caixas de som, com o Reinaldo interrompendo (eco-fala) ou calado (eco-calado), e ao
lado o áudio exato que saiu nas caixas (*.ref.wav, alinhado: o eco chega ~289 ms depois).

Processa em quadros de 10 ms, como o cliente faria, e mede:
- ERLE nas falas caladas: quanto o eco foi atenuado (dB; maior é melhor);
- o detector da wake word (ADR-014) depois do AEC: acerto nas interrupções e disparos nas caladas.
  As interrupções nunca entraram no treino do modelo. As caladas entraram como negativos (sem AEC),
  então os disparos nelas são só um indício.

Variantes: sem AEC, LiveKit (AEC3 do WebRTC, com e sem supressão de ruído), pywebrtc-audio e o
cancelamento linear do cliente (agatha_client.aec), começando do zero em cada fala ou persistente
(o mesmo filtro passa por todas as falas em sequência, como numa sessão do cliente).
Salva o áudio processado em /dados/agatha/data/wakeword/aec/<variante>/ para ouvir.

Uso: .venv-wake/bin/python bench/aec_bench.py      (pip install livekit==1.1.20 pywebrtc-audio==0.2.0)
"""

import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
DATA = Path("/dados/agatha/data/wakeword/gravacoes")
OUT = Path("/dados/agatha/data/wakeword/aec")
MODEL = "/dados/agatha/models/wakeword/agatha.onnx"
RESULTS = ROOT / "bench" / "results"
SR = 16000
FRAME = 160  # 10 ms: o tamanho que o módulo de áudio do WebRTC exige
DELAY_MS = 289  # medido nas gravações (stream duplex do gravador)
THRESHOLDS = (0.5, 0.7, 0.9)


def read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def write(path: Path, pcm: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype("<i2").tobytes())


# --- Variantes de AEC ------------------------------------------------------------------

def no_aec(mic, ref, delay_ms):
    return mic


def livekit(noise_suppression: bool):
    def run(mic, ref, delay_ms):
        from livekit import rtc

        apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=noise_suppression,
                                        high_pass_filter=True)
        apm.set_stream_delay_ms(delay_ms)
        out = np.zeros_like(mic)
        for i in range(0, len(mic) - FRAME + 1, FRAME):
            far = rtc.AudioFrame(ref[i : i + FRAME].tobytes(), SR, 1, FRAME)
            apm.process_reverse_stream(far)
            near = rtc.AudioFrame(mic[i : i + FRAME].tobytes(), SR, 1, FRAME)
            apm.process_stream(near)
            out[i : i + FRAME] = np.frombuffer(near.data, dtype=np.int16)
        return out
    return run


def pywebrtc(mic, ref, delay_ms):
    import pywebrtc_audio as pw

    ec = pw.EchoCanceller(sample_rate=SR, num_channels=1, stream_delay_ms=delay_ms)
    out = np.zeros_like(mic)
    for i in range(0, len(mic) - FRAME + 1, FRAME):
        out[i : i + FRAME] = np.asarray(ec.process(mic[i : i + FRAME], ref[i : i + FRAME])).reshape(-1)
    return out


def linear(persistent: bool):
    """Cancelamento linear do cliente; o filtro começa 16 ms antes do atraso medido."""
    state = {}

    def run(mic, ref, delay_ms):
        from agatha_client.aec import EchoCanceller

        if not persistent or "ec" not in state:
            state["ec"] = EchoCanceller(delay_ms=delay_ms - 16)
        ec = state["ec"]
        out = np.concatenate([ec.process(mic[i : i + FRAME], ref[i : i + FRAME]) for i in range(0, len(mic), FRAME)])
        return np.roll(out, -ec.n).clip(-32768, 32767).astype(np.int16)  # tira a latência de 1 bloco
    return run


VARIANTS = {
    "sem-aec": no_aec,
    "livekit": livekit(noise_suppression=False),
    "livekit-ns": livekit(noise_suppression=True),
    "pywebrtc": pywebrtc,
    "linear": linear(persistent=False),
    "linear-persistente": linear(persistent=True),
}


# --- Medidas ---------------------------------------------------------------------------

def erle_db(mic: np.ndarray, out: np.ndarray, ref: np.ndarray, delay_ms: int) -> float:
    """Atenuação do eco onde a Agatha está falando, depois de 1 s de adaptação."""
    d = int(delay_ms * SR / 1000)
    active = np.zeros(len(mic), bool)
    frames = len(ref) // FRAME
    level = np.sqrt((ref[: frames * FRAME].astype(float).reshape(frames, FRAME) ** 2).mean(1))
    for k in np.where(level > 300)[0]:
        active[k * FRAME + d : (k + 1) * FRAME + d] = True
    active[: SR] = False
    if active.sum() < SR // 2:
        return float("nan")
    e = lambda x: float(np.mean(x[active].astype(float) ** 2)) + 1e-9  # noqa: E731
    return 10 * np.log10(e(mic) / e(out))


class Detector:
    def __init__(self):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[MODEL], inference_framework="onnx")

    def scores(self, pcm: np.ndarray) -> np.ndarray:
        self.model.reset()
        x = np.concatenate([np.zeros(SR, np.int16), pcm, np.zeros(SR // 2, np.int16)])
        return np.array([self.model.predict(x[i : i + 1280])["agatha"] for i in range(0, len(x) - 1279, 1280)])


def fires(scores: np.ndarray, thr: float, refractory: int = 25) -> int:
    n, last = 0, -10**9
    for i, s in enumerate(scores):
        if s >= thr and i - last >= refractory:
            n, last = n + 1, i
    return n


def main() -> int:
    rows = {}
    for line in (DATA / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    takes = sorted((r for r in rows.values() if r["part"] == "eco"), key=lambda r: r["id"])
    detector = Detector()
    results = {}
    for name, fn in VARIANTS.items():
        per_take, erles, t_proc, audio_s = [], [], 0.0, 0.0
        for r in takes:
            mic, ref = read(DATA / r["file"]), read(DATA / r["ref_file"])
            n = min(len(mic), len(ref)) // FRAME * FRAME
            mic, ref = mic[:n], ref[:n]
            t0 = time.perf_counter()
            out = fn(mic, ref, DELAY_MS)
            t_proc += time.perf_counter() - t0
            audio_s += n / SR
            write(OUT / name / f"{r['id']}.wav", out)
            sc = detector.scores(out)
            entry = {"id": r["id"], "label": r["label"], "text": r["text"], "max": round(float(sc.max()), 3)}
            if r["label"] == "echo_only":
                entry["erle_db"] = round(erle_db(mic, out, ref, DELAY_MS), 1)
                erles.append(entry["erle_db"])
            entry["fires"] = {str(t): fires(sc, t) for t in THRESHOLDS}
            per_take.append(entry)
        wake = [e for e in per_take if e["label"] == "echo_wake"]
        quiet = [e for e in per_take if e["label"] == "echo_only"]
        results[name] = {
            "erle_db_median": round(float(np.nanmedian(erles)), 1) if name != "sem-aec" else 0.0,
            "recall": {str(t): round(np.mean([e["max"] >= t for e in wake]), 3) for t in THRESHOLDS},
            "quiet_fires": {str(t): sum(e["fires"][str(t)] for e in quiet) for t in THRESHOLDS},
            "ms_per_s_audio": round(1000 * t_proc / audio_s, 1),
            "takes": per_take,
        }
        print(f"  {name}: pronto", file=sys.stderr, flush=True)

    lines = [f"| variante | ERLE (mediana, dB) | acerto nas interrupções ({', '.join(THRESHOLDS.__str__().strip('()').split(', '))}) "
             f"| disparos nas caladas | CPU (ms por s de áudio) |", "|---|---|---|---|---|"]
    for name, r in results.items():
        rec = " / ".join(f"{v:.0%}" for v in r["recall"].values())
        quiet = " / ".join(str(v) for v in r["quiet_fires"].values())
        lines.append(f"| {name} | {r['erle_db_median']} | {rec} | {quiet} | {r['ms_per_s_audio']} |")
    n_wake = sum(1 for t in takes if t["label"] == "echo_wake")
    table = "\n".join(lines)
    print(table)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"aec-{stamp}"
    out.with_suffix(".md").write_text(
        f"# Cancelamento de eco ({stamp})\n\n{len(takes)} gravações de eco ({n_wake} com interrupção), "
        f"atraso informado ao AEC: {DELAY_MS} ms. Limiares do detector: {THRESHOLDS}.\n\n{table}\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
