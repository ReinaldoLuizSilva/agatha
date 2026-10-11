"""Cliente da Agatha (Fase 1): envia o microfone ao servidor e mostra as transcrições.

Uso:
    python -m agatha_client                       # microfone padrão
    python -m agatha_client --list-devices        # lista os dispositivos de entrada
    python -m agatha_client --device 3            # escolhe o microfone
    python -m agatha_client --wav frase.wav       # envia um arquivo em vez do microfone

O token vem de --token, da variável AGATHA_TOKEN ou do .env do diretório atual.
Protocolo: docs/protocol.md (versão 1).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI

from . import __version__

PROTOCOL_VERSION = 1
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # 32 ms, o bloco nativo do VAD do servidor
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1}
DEFAULT_URL = "ws://192.168.1.25:8765/"
RETRY_S = 3.0
# Erros em que tentar de novo não adianta
FATAL_ERRORS = {"unauthorized", "unsupported_protocol", "unsupported_audio"}


class FatalError(Exception):
    pass


# --- Fontes de áudio ---------------------------------------------------------------

def _sounddevice():
    try:
        import sounddevice
    except OSError as e:  # PortAudio ausente; não confundir com erro de rede
        raise FatalError(f"não foi possível carregar o áudio (PortAudio): {e}")
    return sounddevice


class MicSource:
    """Microfone em blocos de 512 amostras s16le, 16 kHz, mono.

    O PortAudio chama o callback numa thread própria; os blocos passam para o asyncio por uma fila.
    """

    finite = False

    def __init__(self, device: int | str | None):
        self.device = device
        self.check()

    def check(self) -> None:
        """Falha logo, antes de conectar, se o microfone não aceitar 16 kHz mono."""
        sd = _sounddevice()
        try:
            sd.check_input_settings(device=self.device, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        except (sd.PortAudioError, ValueError) as e:
            raise FatalError(
                f"o microfone não aceita 16 kHz mono: {e}\n"
                "Veja os dispositivos com --list-devices e escolha outro com --device "
                "(as entradas 'MME' costumam aceitar 16 kHz)."
            )

    async def frames(self) -> AsyncIterator[bytes]:
        sd = _sounddevice()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def callback(indata, frames, time_info, status):
            if status.input_overflow:
                loop.call_soon_threadsafe(print, "  (aviso: perda de áudio na captura)")
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=callback,
            )
        except sd.PortAudioError as e:
            raise FatalError(f"não foi possível abrir o microfone: {e}")
        with stream:
            while True:
                yield await queue.get()


class WavSource:
    """Arquivo WAV de 16 kHz enviado no ritmo real, seguido de 1,5 s de silêncio."""

    finite = True

    def __init__(self, path: str):
        try:
            import numpy as np
            import soundfile as sf
        except ImportError:
            raise FatalError("--wav precisa do soundfile: pip install -e \".[wav]\"")
        # Lê em float e converte à mão (pedir int16 de um WAV float não reescala).
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate != SAMPLE_RATE:
            raise FatalError(f"{path}: {rate} Hz (esperado {SAMPLE_RATE} Hz)")
        pcm = np.clip(np.round(audio.mean(axis=1) * 32768), -32768, 32767).astype("<i2").tobytes()
        pcm += bytes(2 * int(SAMPLE_RATE * 1.5))
        step = 2 * FRAME_SAMPLES
        self._chunks = [pcm[i : i + step] for i in range(0, len(pcm) - len(pcm) % step, step)]

    async def frames(self) -> AsyncIterator[bytes]:
        t0 = time.monotonic()
        for i, chunk in enumerate(self._chunks):
            yield chunk
            delay = t0 + (i + 1) * FRAME_SAMPLES / SAMPLE_RATE - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)


# --- Sessão --------------------------------------------------------------------------

def show(msg: dict, ended: set[int], done: set[int]) -> None:
    if msg["type"] == "vad":
        if msg["event"] == "start":
            print(f"  ▶ ouvindo... (#{msg['segment']})", flush=True)
        else:
            ended.add(msg["segment"])
    elif msg["type"] == "transcript":
        done.add(msg["segment"])
        t = msg["timings"]
        text = msg["text"] or "(nada reconhecido)"
        print(f"  » {text}", flush=True)
        print(f"    stt {t['stt_ms']} ms · total {t['total_ms']} ms (fim da fala → texto)", flush=True)
    elif msg["type"] == "error":
        print(f"  ! erro do servidor: {msg['code']}: {msg['message']}", flush=True)


async def session(url: str, token: str, source: MicSource | WavSource) -> None:
    async with connect(url, open_timeout=5) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "client": f"agatha-client/{__version__} ({sys.platform})",
            "token": token,
            "audio": AUDIO_FORMAT,
        }))
        reply = json.loads(await ws.recv())
        if reply["type"] == "error":
            message = f"servidor recusou a conexão: {reply['code']}: {reply['message']}"
            if reply["code"] in FATAL_ERRORS:
                raise FatalError(message)
            raise ConnectionError(message)
        print(f"conectado a {url} (sessão {reply['session']})")
        print("Pode falar. Ctrl+C para sair.\n" if not source.finite else "Enviando o arquivo...\n")

        ended: set[int] = set()
        done: set[int] = set()

        async def receive():
            async for raw in ws:
                show(json.loads(raw), ended, done)

        async def send():
            async for frame in source.frames():
                await ws.send(frame)

        receiver = asyncio.create_task(receive())
        sender = asyncio.create_task(send())
        finished, _ = await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
        if sender in finished and sender.exception() is None:
            # arquivo terminou: espera as transcrições das frases já encerradas
            deadline = time.monotonic() + 10
            while ended - done and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        for task in (receiver, sender):
            task.cancel()
        for task in finished:
            if task.exception() is not None:
                raise task.exception()
        if receiver in finished:
            raise ConnectionError("o servidor encerrou a conexão")


async def run(url: str, token: str, source: MicSource | WavSource) -> None:
    while True:
        try:
            await session(url, token, source)
            if source.finite:
                return
        except (ConnectionClosed, ConnectionError, OSError, TimeoutError) as e:
            if source.finite:
                raise FatalError(f"conexão falhou: {e}")
            print(f"conexão com {url} indisponível ({e or type(e).__name__}); nova tentativa em {RETRY_S:g} s")
        await asyncio.sleep(RETRY_S)


# --- CLI -----------------------------------------------------------------------------

def read_env_file(path: Path) -> dict:
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    return env


def list_devices() -> None:
    sd = _sounddevice()

    default_input = sd.default.device[0]
    hostapis = sd.query_hostapis()
    print("Dispositivos de entrada (* = padrão):")
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            mark = "*" if i == default_input else " "
            print(f" {mark} {i:3d}  {dev['name']}  [{hostapis[dev['hostapi']]['name']}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help=f"endereço do servidor (padrão: AGATHA_URL ou {DEFAULT_URL})")
    parser.add_argument("--token")
    parser.add_argument("--device", help="índice ou nome do microfone (veja --list-devices)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--wav", help="envia este arquivo WAV (16 kHz) em vez do microfone")
    args = parser.parse_args()

    try:
        if args.list_devices:
            list_devices()
            return 0
    except FatalError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1

    env = {**read_env_file(Path(".env")), **os.environ}
    url = args.url or env.get("AGATHA_URL") or DEFAULT_URL
    token = args.token or env.get("AGATHA_TOKEN")
    if not token:
        print("token não informado (--token, AGATHA_TOKEN ou .env)", file=sys.stderr)
        return 2
    device = int(args.device) if args.device and args.device.isdigit() else args.device

    try:
        source = WavSource(args.wav) if args.wav else MicSource(device)
        asyncio.run(run(url, token, source))
    except FatalError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    except InvalidURI as e:
        print(f"erro: endereço inválido: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\naté logo!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
