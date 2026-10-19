"""Amostras de voz para o teste de audição da Fase 7 (ADR-019).

Gera as mesmas frases típicas da Agatha em várias versões (a dii como está, a dii com os
parâmetros de síntese ajustados, a pf_dora do Kokoro), com o texto normalizado exatamente como o
servidor faz (`agatha_server.text`). Mede também o tempo até o áudio da frase curta, no CPU.

Roda no venv do TTS (dependências do Piper e do Kokoro separadas do servidor):
    .venv-tts/bin/python bench/voice_samples.py --out /dados/agatha/data/tts/amostras
Os WAV ficam fora do git; o resumo vai para bench/results/voice-samples-*.json.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
from agatha_server.audio import resample  # noqa: E402
from agatha_server.text import clean, normalize_for_speech  # noqa: E402

MODELS = Path("/dados/agatha/models/tts")
DII = MODELS / "piper" / "dii_pt-BR.onnx"

SENTENCES = [
    "São 21h02 de sexta-feira, Rei.",
    "Deixa eu ver. Tem um problema no agatha-srv: o disco do /dados está com 85% de uso.",
    "O RMAN é o utilitário de backup e recuperação do Oracle. Ele faz backups consistentes e restaura o banco "
    "em caso de falha.",
    "De nada, Reinaldo. Qualquer coisa, é só chamar.",
]

# nome → (descrição, motor, parâmetros, taxa de saída ou None para a nativa)
VERSIONS = {
    "dii-atual": ("dii como toca hoje (16 kHz, modo barge-in)", "piper", {}, 16000),
    "dii-22k": ("dii na qualidade original (22 kHz, como antes do barge-in)", "piper", {}, None),
    "dii-limpa": ("dii com menos ruído na síntese (noise 0,4 / 0,5)", "piper",
                  {"noise_scale": 0.4, "noise_w_scale": 0.5}, None),
    "dii-mais-limpa": ("dii com bem menos ruído (noise 0,25 / 0,3)", "piper",
                       {"noise_scale": 0.25, "noise_w_scale": 0.3}, None),
    "kokoro-dora": ("Kokoro pf_dora (24 kHz, Apache 2.0)", "kokoro", {}, None),
}


class Piper:
    def __init__(self):
        from piper import PiperVoice

        self.voice = PiperVoice.load(str(DII))
        self.rate = self.voice.config.sample_rate

    def synth(self, text: str, params: dict) -> np.ndarray:
        from piper import SynthesisConfig

        pcm = b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text, SynthesisConfig(**params)))
        return np.frombuffer(pcm, dtype="<i2")


class Kokoro:
    def __init__(self):
        from kokoro_onnx import Kokoro as K

        self.model = K(str(MODELS / "kokoro" / "kokoro-v1.0.onnx"), str(MODELS / "kokoro" / "voices-v1.0.bin"))
        self.rate = 24000

    def synth(self, text: str, params: dict) -> np.ndarray:
        audio, rate = self.model.create(text, voice="pf_dora", lang="pt-br")
        assert rate == self.rate
        return np.clip(np.round(audio * 32767), -32768, 32767).astype("<i2")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("/dados/agatha/data/tts/amostras"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    engines = {"piper": Piper(), "kokoro": Kokoro()}
    texts = [normalize_for_speech(clean(s)) for s in SENTENCES]
    for e in engines.values():  # aquecimento: a 1ª síntese inclui a carga do modelo
        e.synth("Oi.", {})

    summary = {"date": datetime.now().isoformat(timespec="seconds"), "sentences": SENTENCES, "spoken": texts,
               "versions": {}}
    for name, (desc, engine, params, out_rate) in VERSIONS.items():
        tts = engines[engine]
        rate = out_rate or tts.rate
        times = []
        for i, text in enumerate(texts, 1):
            t0 = time.perf_counter()
            pcm = tts.synth(text, params)
            times.append(time.perf_counter() - t0)
            if out_rate and out_rate != tts.rate:
                pcm = np.frombuffer(resample(pcm.tobytes(), tts.rate, out_rate), dtype="<i2")
            sf.write(args.out / f"{name}-{i}.wav", pcm, rate, subtype="PCM_16")
        short = times[-1]  # frase curta: o que pesa no 1º áudio
        summary["versions"][name] = {"description": desc, "rate": rate, "params": params,
                                     "short_sentence_ms": round(short * 1000), "synth_s": [round(t, 3) for t in times]}
        print(f"{name:15s} {rate} Hz · frase curta {short * 1000:5.0f} ms · {desc}", flush=True)

    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    out = ROOT / "bench" / "results" / f"voice-samples-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nfalado:", *texts, sep="\n  ")
    print(f"amostras em {args.out}; resumo em {out}")


if __name__ == "__main__":
    main()
