"""Cliente de teste (protocolo v2): conversa com a Agatha usando arquivos WAV no lugar do microfone.

Cada arquivo é enviado como uma fala, em blocos de 32 ms no ritmo real, seguido de silêncio.
Quando a Agatha responde, o envio pausa (half-duplex), a reprodução é simulada pelo tempo do
áudio e o script manda o `playback_end` com o `e2e_ms` (fim da fala → primeiro áudio recebido),
medido no próprio relógio como o cliente real faz (sem a latência da placa de som).

Uso:
    python scripts/send_wav.py pergunta1.wav pergunta2.wav
    python scripts/send_wav.py --save-replies /tmp/respostas --out resultado.json perguntas/*.wav

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
from agatha_server.vad import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


def load_pcm(path: str) -> bytes:
    # Lê em float e converte à mão: pedir int16 de um WAV float ao libsndfile não reescala
    # (amostras em [-1, 1] viram 0).
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if rate != SAMPLE_RATE:
        sys.exit(f"{path}: {rate} Hz (esperado {SAMPLE_RATE} Hz; converta antes)")
    mono = audio.mean(axis=1)  # mixa para mono se preciso
    return np.clip(np.round(mono * 32768), -32768, 32767).astype("<i2").tobytes()


class SimulatedClient:
    def __init__(self, ws, args):
        self.ws = ws
        self.args = args
        self.sent_at: list[float] = []  # instante de envio de cada bloco de 32 ms
        self.can_send = asyncio.Event()
        self.can_send.set()
        self.speech_end_ms: dict[int, int] = {}
        self.transcripts: list[dict] = []
        self.responses: list[dict] = []
        self.output_rate = 22050
        self._reply: dict | None = None
        self._pending_segments: set[int] = set()

    async def send(self, chunks: list[bytes]) -> None:
        t0 = time.monotonic()
        for chunk in chunks:
            if not self.can_send.is_set():  # half-duplex: não envia enquanto ela responde
                await self.can_send.wait()
                t0 = time.monotonic() - len(self.sent_at) * FRAME_S / self.args.speed
            await self.ws.send(chunk)
            self.sent_at.append(time.monotonic())
            delay = t0 + len(self.sent_at) * FRAME_S / self.args.speed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    def busy(self) -> bool:
        return bool(self._pending_segments) or not self.can_send.is_set()

    async def receive(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, bytes):
                await self._on_audio(raw)
            else:
                await self._on_message(json.loads(raw))

    async def _on_message(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "vad":
            if msg["event"] == "start":
                print(f"  ▶ #{msg['segment']} fala @ {msg['audio_ms'] / 1000:.2f}s", flush=True)
                self._pending_segments.add(msg["segment"])
            else:
                self.speech_end_ms[msg["segment"]] = msg["speech_end_ms"]
        elif kind == "transcript":
            t = msg["timings"]
            print(f"  ✎ #{msg['segment']} {msg['text']!r} (stt {t['stt_ms']} ms · total {t['total_ms']} ms)", flush=True)
            self.transcripts.append(msg)
            if not msg["text"]:
                self._pending_segments.discard(msg["segment"])
        elif kind == "response_start":
            self.can_send.clear()
            self._reply = {"response": msg["response"], "segment": msg["segment"], "audio": bytearray(),
                           "first_audio_at": None, "texts": []}
        elif kind == "response_text":
            self._reply["texts"].append(msg["text"])
            print(f"  » {msg['text']}", flush=True)
        elif kind == "audio_start":
            self.output_rate = msg["sample_rate"]
        elif kind == "response_end":
            await self._finish_reply(msg)
        elif kind == "error":
            print(f"  ! {msg}", flush=True)

    async def _on_audio(self, data: bytes) -> None:
        reply = self._reply
        if reply["first_audio_at"] is None:
            reply["first_audio_at"] = time.monotonic()
        reply["audio"] += data

    async def _finish_reply(self, msg: dict) -> None:
        reply, self._reply = self._reply, None
        audio_s = len(reply["audio"]) / 2 / self.output_rate
        e2e_ms = None
        speech_end = self.speech_end_ms.get(reply["segment"])
        if reply["first_audio_at"] and speech_end:
            # bloco que termina em speech_end_ms (posições contam os blocos enviados)
            captured_at = self.sent_at[speech_end // FRAME_MS - 1]
            e2e_ms = round((reply["first_audio_at"] - captured_at) * 1000)
            # simula a reprodução: espera o áudio "tocar" até o fim
            remaining = reply["first_audio_at"] + audio_s - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining / self.args.speed)
        t = msg["timings"]
        print(f"  ■ resposta {msg['response']} ({msg['reason']}): {audio_s:.1f}s de áudio · "
              f"1º áudio {t.get('first_audio_ms')} ms (servidor) · fim da fala → 1º áudio {e2e_ms} ms (cliente)",
              flush=True)
        if self.args.save_replies and reply["audio"]:
            self.args.save_replies.mkdir(parents=True, exist_ok=True)
            path = self.args.save_replies / f"resposta-{msg['response']:02d}.wav"
            sf.write(path, np.frombuffer(bytes(reply["audio"]), dtype="<i2"), self.output_rate)
        await self.ws.send(json.dumps({"type": "playback_end", "response": msg["response"], "e2e_ms": e2e_ms}))
        self.responses.append({**msg, "e2e_ms": e2e_ms, "audio_s": audio_s})
        self._pending_segments.discard(reply["segment"])
        self.can_send.set()


def summary(client: SimulatedClient) -> None:
    def table(rows: list[dict], keys: list[str], get) -> None:
        print("| métrica | p50 | p90 | máx |\n|---|---|---|---|")
        for key in keys:
            values = sorted(v for r in rows if (v := get(r, key)) is not None)
            if values:
                p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
                print(f"| {key} | {statistics.median(values):.0f} | {p90} | {values[-1]} |")

    print(f"\n{len(client.transcripts)} frases transcritas, {len(client.responses)} respostas")
    if client.responses:
        table(client.responses, ["stt_ms", "llm_first_token_ms", "first_chunk_ms", "tts_first_ms", "first_audio_ms"],
              lambda r, k: r["timings"].get(k))
        table(client.responses, ["e2e_ms"], lambda r, k: r.get(k))


async def main_async(args) -> None:
    silence = bytes(2 * int(SAMPLE_RATE * args.gap_ms / 1000))
    pcm = silence + b"".join(load_pcm(f) + silence for f in args.files)
    step = 2 * FRAME_SAMPLES
    chunks = [pcm[i : i + step] for i in range(0, len(pcm) - len(pcm) % step, step)]
    print(f"{len(args.files)} arquivo(s), {len(chunks) * FRAME_S:.1f}s de áudio, velocidade {args.speed:g}x")

    async with connect(args.url, max_size=2**22) as ws:
        await ws.send(json.dumps({
            "type": "hello", "protocol": PROTOCOL_VERSION, "client": "send_wav",
            "token": args.token, "audio": AUDIO_FORMAT,
        }))
        ready = json.loads(await ws.recv())
        if ready["type"] != "ready":
            sys.exit(f"recusado: {ready}")
        print(f"sessão {ready['session']} · resposta em {ready['output_audio']['sample_rate']} Hz\n")

        client = SimulatedClient(ws, args)
        receiver = asyncio.create_task(client.receive())
        await client.send(chunks)
        deadline = time.monotonic() + args.wait_s
        while client.busy() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        receiver.cancel()
    summary(client)
    if args.out:
        args.out.write_text(json.dumps({
            "date": datetime.now().isoformat(timespec="seconds"), "url": args.url, "speed": args.speed,
            "gap_ms": args.gap_ms, "files": args.files,
            "transcripts": client.transcripts, "responses": client.responses,
        }, ensure_ascii=False, indent=2))
        print(f"\nresultados salvos em {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/")
    parser.add_argument("--token")
    parser.add_argument("--speed", type=float, default=1.0, help="1 = tempo real")
    parser.add_argument("--gap-ms", type=int, default=1500, help="silêncio depois de cada arquivo")
    parser.add_argument("--wait-s", type=float, default=30.0, help="espera final pelas respostas")
    parser.add_argument("--save-replies", type=Path, help="pasta para salvar o áudio das respostas")
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
