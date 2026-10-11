"""Cliente da Agatha (Fase 2): conversa por voz com o servidor.

Uso:
    python -m agatha_client                       # microfone e saída de som padrão
    python -m agatha_client --list-devices        # lista os dispositivos de entrada e saída
    python -m agatha_client --device 3            # escolhe o microfone
    python -m agatha_client --output-device 5     # escolhe a saída de som
    python -m agatha_client --wav frase.wav       # envia um arquivo em vez do microfone
    python -m agatha_client --wav frase.wav --no-playback   # sem saída de som (testes)

O token vem de --token, da variável AGATHA_TOKEN ou do .env do diretório atual.
Protocolo: docs/protocol.md (versão 2: respostas faladas, half-duplex).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI

from . import __version__
from .audio import FRAME_S, SAMPLE_RATE, FatalError, MicSource, NullPlayer, Player, WavSource, list_devices

PROTOCOL_VERSION = 2
FRAME_MS = round(FRAME_S * 1000)  # 32
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1}
DEFAULT_URL = "ws://192.168.1.25:8765/"
RETRY_S = 3.0
# Erros em que tentar de novo não adianta
FATAL_ERRORS = {"unauthorized", "unsupported_protocol", "unsupported_audio"}


class Session:
    def __init__(self, ws, source: MicSource | WavSource, player: Player | NullPlayer, wake: dict | None = None):
        self.ws = ws
        self.wake = wake or {}
        self.source = source
        self.player = player
        self.can_send = asyncio.Event()  # limpo durante uma resposta (half-duplex)
        self.can_send.set()
        self.captured_at: list[float] = []  # instante de captura de cada bloco enviado
        self.speech_end_ms: dict[int, int] = {}
        self.pending: set[int] = set()  # frases ainda sem resposta (para o modo --wav esperar)
        self._reply: dict | None = None
        self._tasks: set[asyncio.Task] = set()

    async def send_loop(self) -> None:
        async for pcm, captured_at in self.source.frames():
            if not self.can_send.is_set():
                if not self.source.finite:
                    continue  # microfone: descarta o que foi dito durante a resposta
                await self.can_send.wait()  # arquivo: pausa para não perder pedaços
                captured_at = time.monotonic()
            await self.ws.send(pcm)
            self.captured_at.append(captured_at)

    async def receive_loop(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, bytes):
                if self._reply is not None:
                    self.player.feed(raw)
                continue
            self._on_message(json.loads(raw))

    def _on_message(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "vad":
            if msg["event"] == "start":
                self.pending.add(msg["segment"])
                print(f"  ▶ ouvindo... (#{msg['segment']})", flush=True)
            else:
                self.speech_end_ms[msg["segment"]] = msg["speech_end_ms"]
        elif kind == "transcript":
            t = msg["timings"]
            if msg.get("addressed", True):
                print(f"  #{msg['segment']} você: {msg['text'] or '(nada reconhecido)'}  "
                      f"[stt {t['stt_ms']} ms]", flush=True)
            else:  # não chamou a Agatha (wake word): ela não vai responder
                print(f"  · ignorado (sem \"Agatha\"): {msg['text']}", flush=True)
            if not msg["text"] or not msg.get("addressed", True):
                self.pending.discard(msg["segment"])
        elif kind == "response_start":
            self.can_send.clear()
            self.player.begin()
            self._reply = {"response": msg["response"], "segment": msg["segment"]}
            print("  agatha: ", end="", flush=True)
        elif kind == "response_text":
            print(msg["text"], end=" ", flush=True)
        elif kind == "audio_end":
            self.player.end()
        elif kind == "response_end":
            task = asyncio.create_task(self._finish_reply(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "error":
            print(f"\n  ! erro do servidor: {msg['code']}: {msg['message']}", flush=True)

    async def _finish_reply(self, msg: dict) -> None:
        reply, self._reply = self._reply, None
        self.player.end()  # sem áudio (erro): libera na hora
        first_played_at = await self.player.wait_done()
        e2e_ms = None
        speech_end = self.speech_end_ms.get(reply["segment"])
        if first_played_at is not None and speech_end:
            # bloco que termina em speech_end_ms: as posições contam os blocos enviados
            e2e_ms = round((first_played_at - self.captured_at[speech_end // FRAME_MS - 1]) * 1000)
        await self.ws.send(json.dumps({"type": "playback_end", "response": reply["response"], "e2e_ms": e2e_ms}))
        server_ms = msg["timings"].get("first_audio_ms")
        detail = f" (servidor {server_ms} ms + rede/reprodução {e2e_ms - server_ms} ms)" if e2e_ms and server_ms else ""
        status = "" if msg["reason"] == "done" else f" [{msg['reason']}]"
        print(f"\n    ⏱ fim da fala → primeiro som: {e2e_ms} ms{detail}{status}", flush=True)
        if self.wake.get("enabled"):
            print(f"    (pode continuar sem dizer \"Agatha\" por {self.wake['conversation_s']:g} s)", flush=True)
        print(flush=True)
        self.pending.discard(reply["segment"])
        self.can_send.set()

    def busy(self) -> bool:
        return bool(self.pending) or not self.can_send.is_set()


async def session(url: str, token: str, source: MicSource | WavSource, args) -> None:
    async with connect(url, open_timeout=5, max_size=2**22) as ws:
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
        rate = reply["output_audio"]["sample_rate"]
        player = NullPlayer(rate) if args.no_playback else Player(args.output_device, rate)
        print(f"conectado a {url} (sessão {reply['session']}, resposta a {rate} Hz, "
              f"latência de saída {player.latency * 1000:.0f} ms)")
        wake = reply.get("wake") or {}
        if source.finite:
            print("Enviando o arquivo...\n")
        elif wake.get("enabled"):
            print("Comece chamando \"Agatha\" (ex.: \"Agatha, que horas são?\"). Ctrl+C para sair.\n")
        else:
            print("Pode falar. Ctrl+C para sair.\n")

        s = Session(ws, source, player, wake)
        receiver = asyncio.create_task(s.receive_loop())
        sender = asyncio.create_task(s.send_loop())
        try:
            finished, _ = await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in finished and sender.exception() is None:
                # arquivo terminou: espera as respostas das frases já detectadas
                deadline = time.monotonic() + 30
                while s.busy() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
            for task in finished:
                if task.exception() is not None:
                    raise task.exception()
            if receiver in finished:
                raise ConnectionError("o servidor encerrou a conexão")
        except asyncio.CancelledError:
            # Ctrl+C: fecha normalmente (1000); sair do `async with` com exceção fecharia com 1011
            await ws.close()
            raise
        finally:
            tasks = [receiver, sender, *s._tasks]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            player.close()


async def run(url: str, token: str, source: MicSource | WavSource, args) -> None:
    while True:
        try:
            await session(url, token, source, args)
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


def device_arg(value: str | None) -> int | str | None:
    return int(value) if value and value.isdigit() else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help=f"endereço do servidor (padrão: AGATHA_URL ou {DEFAULT_URL})")
    parser.add_argument("--token")
    parser.add_argument("--device", help="índice ou nome do microfone (veja --list-devices)")
    parser.add_argument("--output-device", help="índice ou nome da saída de som (veja --list-devices)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--wav", help="envia este arquivo WAV (16 kHz) em vez do microfone")
    parser.add_argument("--no-playback", action="store_true", help="não toca as respostas (simula o tempo)")
    args = parser.parse_args()
    args.device = device_arg(args.device)
    args.output_device = device_arg(args.output_device)

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

    try:
        source = WavSource(args.wav) if args.wav else MicSource(args.device)
        asyncio.run(run(url, token, source, args))
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
