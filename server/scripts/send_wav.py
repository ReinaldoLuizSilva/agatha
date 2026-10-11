"""Cliente de teste: transmite arquivos WAV ao servidor como se fosse um microfone.

Os arquivos são enviados em blocos de 32 ms no ritmo real (ou mais rápido, com --speed), com
silêncio entre eles, e o script mostra os eventos recebidos e um resumo das latências.

Uso:
    python scripts/send_wav.py arquivo1.wav arquivo2.wav
    python scripts/send_wav.py --url ws://192.168.1.25:8765/ /dados/agatha/data/fleurs/pt_br/dev/*.wav

O token vem de --token, da variável AGATHA_TOKEN ou do .env do diretório atual.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from websockets.asyncio.client import connect

from agatha_server.config import load_env_file
from agatha_server.protocol import AUDIO_FORMAT, PROTOCOL_VERSION
from agatha_server.vad import FRAME_SAMPLES, SAMPLE_RATE

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


def load_pcm(path: str) -> bytes:
    # Lê em float e converte à mão: pedir int16 de um WAV float ao libsndfile não reescala
    # (amostras em [-1, 1] viram 0).
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if rate != SAMPLE_RATE:
        sys.exit(f"{path}: {rate} Hz (esperado {SAMPLE_RATE} Hz; converta antes)")
    mono = audio.mean(axis=1)  # mixa para mono se preciso
    return np.clip(np.round(mono * 32768), -32768, 32767).astype("<i2").tobytes()


async def send_audio(ws, chunks: list[bytes], speed: float) -> None:
    """Envia bloco a bloco, compensando o atraso acumulado para manter o ritmo."""
    t0 = time.monotonic()
    for i, chunk in enumerate(chunks):
        await ws.send(chunk)
        delay = t0 + (i + 1) * FRAME_S / speed - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


async def receive(ws, results: list[dict], ended: set[int]) -> None:
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "vad":
            mark = "▶" if msg["event"] == "start" else "■"
            print(f"  {mark} #{msg['segment']} {msg['event']} @ {msg['audio_ms'] / 1000:.2f}s", flush=True)
            if msg["event"] == "end":
                ended.add(msg["segment"])
        elif msg["type"] == "transcript":
            t = msg["timings"]
            print(
                f"  ✎ #{msg['segment']} {msg['text']!r}\n"
                f"     fala {t['speech_ms'] / 1000:.1f}s · endpoint {t['endpoint_ms']} ms · "
                f"fila {t['queue_ms']} ms · stt {t['stt_ms']} ms · total {t['total_ms']} ms",
                flush=True,
            )
            results.append(msg)
        else:
            print(f"  ! {msg}", flush=True)


def save(results: list[dict], args, out: Path) -> None:
    out.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "url": args.url,
        "speed": args.speed,
        "gap_ms": args.gap_ms,
        "files": args.files,
        "results": results,
    }, ensure_ascii=False, indent=2))
    print(f"\nresultados salvos em {out}")


def summary(results: list[dict]) -> None:
    if not results:
        print("\nnenhuma transcrição recebida")
        return
    print(f"\n{len(results)} frases transcritas")
    print("| métrica | p50 | p90 | máx |\n|---|---|---|---|")
    for key in ("endpoint_ms", "queue_ms", "stt_ms", "total_ms"):
        values = sorted(r["timings"][key] for r in results)
        p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
        print(f"| {key} | {statistics.median(values):.0f} | {p90} | {values[-1]} |")


async def main_async(args) -> None:
    silence = bytes(2 * int(SAMPLE_RATE * args.gap_ms / 1000))
    pcm = silence + b"".join(load_pcm(f) + silence for f in args.files)
    step = 2 * FRAME_SAMPLES
    chunks = [pcm[i : i + step] for i in range(0, len(pcm) - len(pcm) % step, step)]
    print(f"{len(args.files)} arquivo(s), {len(chunks) * FRAME_S:.1f}s de áudio, velocidade {args.speed:g}x")

    async with connect(args.url) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "client": "send_wav",
            "token": args.token,
            "audio": AUDIO_FORMAT,
        }))
        ready = json.loads(await ws.recv())
        if ready["type"] != "ready":
            sys.exit(f"recusado: {ready}")
        print(f"sessão {ready['session']} · VAD {ready['vad']}\n")

        results: list[dict] = []
        ended: set[int] = set()
        receiver = asyncio.create_task(receive(ws, results, ended))
        await send_audio(ws, chunks, args.speed)

        # espera as transcrições das frases já encerradas
        deadline = time.monotonic() + args.wait_s
        while len(results) < len(ended) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        receiver.cancel()
    summary(results)
    if args.out:
        save(results, args, args.out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/")
    parser.add_argument("--token")
    parser.add_argument("--speed", type=float, default=1.0, help="1 = tempo real")
    parser.add_argument("--gap-ms", type=int, default=1000, help="silêncio entre arquivos")
    parser.add_argument("--wait-s", type=float, default=10.0, help="espera final pelas transcrições")
    parser.add_argument("--out", type=Path, help="salva os resultados em JSON")
    args = parser.parse_args()

    env = dict(os.environ)
    load_env_file(Path(".env"), env)
    args.token = args.token or env.get("AGATHA_TOKEN")
    if not args.token:
        sys.exit("token não informado (--token, AGATHA_TOKEN ou .env)")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
