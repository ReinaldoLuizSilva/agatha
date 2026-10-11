"""Benchmark de TTS na CPU: vozes PT-BR do Piper e do Kokoro.

Mede, para cada voz, o tempo de síntese de frases típicas da Agatha (com destaque para a
primeira frase curta, que define quando o áudio começa a tocar), a duração do áudio gerado e o
RTF (tempo de síntese ÷ duração do áudio). Salva os WAVs para avaliação de ouvido.

Roda num venv próprio (as dependências do Piper e do Kokoro não se misturam com o servidor):
    python3 -m venv .venv-tts && .venv-tts/bin/pip install -r bench/requirements-tts.txt
    .venv-tts/bin/python bench/tts_bench.py
"""

import argparse
import json
import os
import platform
import statistics
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

MODELS_DIR = Path("/dados/agatha/models/tts")
RESULTS_DIR = Path(__file__).parent / "results"
SAMPLES_DIR = RESULTS_DIR / "tts-samples"  # WAVs ficam fora do git (*.wav no .gitignore)

# Frases no estilo do prompt v2 (ADR-004). A primeira é o caso do "primeiro trecho curto".
SENTENCES = [
    ("curta", "Oi, Rei. Tudo certo por aqui."),
    ("oracle", "Claro. Uma tablespace é um espaço lógico de armazenamento no Oracle, que agrupa tabelas e índices."),
    ("zabbix", "O trigger do Zabbix disparou às quatorze e trinta, indicando uso de CPU acima de noventa por cento "
               "no servidor de banco."),
    ("conta", "Quinze por cento de duzentos e quarenta é trinta e seis."),
    ("diagnóstico", "Vamos investigar isso juntos. Primeiro, verifique se o serviço do agente está rodando, "
                    "depois confira os logs."),
    ("honestidade", "Não tenho acesso à hora atual, mas posso te ajudar com outra coisa."),
    # texto "cru", sem normalização: mostra como cada voz lida com números e símbolos
    ("texto cru", "O servidor 192.168.1.25 está com 85% de disco usado e o Podman 5.8 rodando."),
]


class PiperTTS:
    def __init__(self, model: str):
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(MODELS_DIR / "piper" / model))

    def synth(self, text: str) -> tuple[np.ndarray, int, float]:
        """Devolve (áudio float32, taxa, segundos até o primeiro pedaço de áudio)."""
        t0 = time.perf_counter()
        chunks, rate, first = [], 22050, None
        for chunk in self._voice.synthesize(text):  # um pedaço por frase
            if first is None:
                first = time.perf_counter() - t0
            chunks.append(chunk.audio_float_array)
            rate = chunk.sample_rate
        return np.concatenate(chunks), rate, first


class KokoroTTS:
    def __init__(self, model: str, voice: str):
        from kokoro_onnx import Kokoro

        self._kokoro = Kokoro(str(MODELS_DIR / "kokoro" / model), str(MODELS_DIR / "kokoro" / "voices-v1.0.bin"))
        self._voice = voice

    def synth(self, text: str) -> tuple[np.ndarray, int, float]:
        t0 = time.perf_counter()
        audio, rate = self._kokoro.create(text, voice=self._voice, lang="pt-br")
        return audio, rate, time.perf_counter() - t0  # sem streaming: o 1º áudio sai no fim


VOICES = {
    "piper-dii": lambda: PiperTTS("dii_pt-BR.onnx"),
    "kokoro-dora": lambda: KokoroTTS("kokoro-v1.0.onnx", "pf_dora"),
    "kokoro-dora-int8": lambda: KokoroTTS("kokoro-v1.0.int8.onnx", "pf_dora"),
    "piper-faber": lambda: PiperTTS("pt_BR-faber-medium.onnx"),
}


def run_voice(name: str) -> dict:
    t0 = time.perf_counter()
    tts = VOICES[name]()
    load_s = time.perf_counter() - t0
    tts.synth("Aquecendo.")  # primeira inferência fora da medição

    rows = []
    for i, (label, text) in enumerate(SENTENCES, 1):
        t0 = time.perf_counter()
        audio, rate, first = tts.synth(text)
        elapsed = time.perf_counter() - t0
        duration = len(audio) / rate
        sf.write(SAMPLES_DIR / f"{i:02d}-{label.replace(' ', '_')}-{name}.wav", audio, rate)
        rows.append({
            "label": label, "text": text, "synth_s": elapsed, "first_audio_s": first,
            "audio_s": duration, "rtf": elapsed / duration, "sample_rate": rate,
        })
        print(f"   {label:12s} síntese {elapsed * 1000:5.0f} ms · 1º áudio {first * 1000:5.0f} ms · "
              f"áudio {duration:4.1f} s · RTF {elapsed / duration:.3f}", flush=True)
    return {
        "voice": name,
        "load_s": load_s,
        "first_short_ms": rows[0]["first_audio_s"] * 1000,
        "rtf_median": statistics.median(r["rtf"] for r in rows),
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--voices", default=",".join(VOICES))
    args = parser.parse_args()

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    for name in args.voices.split(","):
        print(f"→ {name}", flush=True)
        runs.append(run_voice(name.strip()))

    out = RESULTS_DIR / f"tts-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "host": platform.node(),
        "cpu_count": os.cpu_count(),
        "runs": runs,
    }, ensure_ascii=False, indent=2))

    print("\n| voz | 1ª frase curta | RTF p50 | carga |\n|---|---|---|---|")
    for r in runs:
        print(f"| {r['voice']} | {r['first_short_ms']:.0f} ms | {r['rtf_median']:.3f} | {r['load_s']:.1f} s |")
    print(f"\nresultados: {out}\namostras:   {SAMPLES_DIR}/")


if __name__ == "__main__":
    main()
