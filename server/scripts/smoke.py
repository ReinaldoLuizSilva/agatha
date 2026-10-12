"""Teste de fumaça ponta a ponta: a Agatha no ar ouve, entende, usa ferramenta e responde em voz.

Gera duas perguntas com o Piper (voz sintética), manda pelo WebSocket como o cliente faria
(scripts/send_wav.py) e confere: transcrição chamando a Agatha, resposta completa com áudio, a
calculadora usada na conta e o primeiro áudio abaixo de --max-e2e-ms. Não toca na memória nem
nos laboratórios (Zabbix, Oracle), para poder rodar a qualquer hora sem efeito colateral.

Uso (da raiz do repositório, com a Agatha no ar):
    .venv/bin/python server/scripts/smoke.py
Sai com código 1 se alguma verificação falhar. Precisa do Piper no venv (bench/requirements-tts.txt).
"""

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

VOICE = "/dados/agatha/models/tts/piper/dii_pt-BR.onnx"
# (texto falado, ferramenta esperada ou None, trecho esperado na resposta ou None)
QUESTIONS = [
    ("Agatha, quanto é quinze por cento de duzentos e quarenta?", "calcular", "36"),
    ("Agatha, para que serve um backup incremental?", None, None),
]


def synthesize(text: str, path: Path, voice) -> None:
    """WAV mono 16 kHz, como o microfone do cliente."""
    pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text))
    rate = voice.config.sample_rate
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    t = np.arange(0, len(x) / rate, 1 / 16000)
    y = np.interp(t, np.arange(len(x)) / rate, x).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(y.tobytes())


def check(results: dict, max_e2e_ms: int) -> list[str]:
    failures = []
    transcripts, responses = results["transcripts"], results["responses"]
    if len(responses) != len(QUESTIONS):
        return [f"esperava {len(QUESTIONS)} respostas, vieram {len(responses)} "
                f"(transcrições: {[t['text'] for t in transcripts]})"]
    for (question, tool, expected), response in zip(QUESTIONS, responses):
        label = question[:45]
        if response["reason"] != "done":
            failures.append(f"{label}: resposta terminou com {response['reason']!r}")
        if not response["audio_s"]:
            failures.append(f"{label}: resposta sem áudio")
        tools = response.get("tools") or []
        if tool and tool not in tools:
            failures.append(f"{label}: esperava a ferramenta {tool}, usou {tools or 'nenhuma'}")
        if not tool and tools:
            failures.append(f"{label}: não esperava ferramenta, usou {tools}")
        if expected and expected not in response["text"]:
            failures.append(f"{label}: resposta sem {expected!r}: {response['text']!r}")
        if response["e2e_ms"] is None or response["e2e_ms"] > max_e2e_ms:
            failures.append(f"{label}: 1º áudio em {response['e2e_ms']} ms (limite {max_e2e_ms} ms)")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env",
                        help="de onde vem o AGATHA_TOKEN")
    parser.add_argument("--max-e2e-ms", type=int, default=3000)
    args = parser.parse_args()

    from piper import PiperVoice

    voice = PiperVoice.load(VOICE)
    with tempfile.TemporaryDirectory(prefix="agatha-smoke-") as tmp:
        wavs = []
        for i, (question, _, _) in enumerate(QUESTIONS, 1):
            path = Path(tmp) / f"{i:02d}.wav"
            synthesize(question, path, voice)
            wavs.append(str(path))
        out = Path(tmp) / "resultado.json"
        # send_wav lê o token do .env do diretório atual
        subprocess.run([sys.executable, str(Path(__file__).with_name("send_wav.py")), "--url", args.url,
                        "--gap-ms", "2500", "--out", str(out), *wavs],
                       cwd=args.env_file.parent, check=True)
        results = json.loads(out.read_text())

    failures = check(results, args.max_e2e_ms)
    print()
    for failure in failures:
        print(f"FALHOU: {failure}")
    print("teste de fumaça: " + ("FALHOU" if failures else "ok"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
