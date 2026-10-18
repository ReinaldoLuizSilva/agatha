"""Cliente da Agatha: conversa por voz com o servidor.

Uso:
    python -m agatha_client                       # microfone e saída de som padrão
    python -m agatha_client --no-barge-in         # sem interromper a Agatha (half-duplex puro)
    python -m agatha_client --list-devices        # lista os dispositivos de entrada e saída
    python -m agatha_client --device 3            # escolhe o microfone
    python -m agatha_client --output-device 5     # escolhe a saída de som
    python -m agatha_client --wav frase.wav       # envia um arquivo em vez do microfone
    python -m agatha_client --wav frase.wav --no-playback   # sem saída de som (testes)

O token vem de --token, da variável AGATHA_TOKEN ou do .env do diretório atual.
Protocolo: docs/protocol.md (versão 2: respostas faladas, half-duplex, barge-in).

Barge-in (ADR-016): diga "Agatha" enquanto ela fala e ela pausa na hora; o servidor confere pela
transcrição e para ("Agatha, para"), responde a uma pergunta nova ou, se não era com ela, retoma.
Precisa do extra [barge-in] e dos modelos em --wake-model; sem eles, o cliente funciona sem
interromper.
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
from .audio import (FRAME_S, SAMPLE_RATE, DuplexAudio, FatalError, MicSource, NullPlayer, Player, WavSource,
                    list_devices)
from .bargein import BargeIn

PROTOCOL_VERSION = 2
FRAME_MS = round(FRAME_S * 1000)  # 32
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1}
DEFAULT_URL = "ws://192.168.1.25:8765/"
RETRY_S = 3.0
# Erros em que tentar de novo não adianta
FATAL_ERRORS = {"unauthorized", "unsupported_protocol", "unsupported_audio"}


class Session:
    def __init__(self, ws, source: MicSource | WavSource | DuplexAudio, player: Player | NullPlayer | DuplexAudio,
                 wake: dict | None = None, barge: BargeIn | None = None):
        self.ws = ws
        self.wake = wake or {}
        self.source = source
        self.player = player
        self.barge = barge  # com barge-in, `source` e `player` são o mesmo DuplexAudio
        self.can_send = asyncio.Event()  # limpo durante uma resposta (half-duplex)
        self.can_send.set()
        self.captured_at: list[float] = []  # instante de captura de cada bloco enviado
        self.speech_end_ms: dict[int, int] = {}
        self.pending: set[int] = set()  # frases ainda sem resposta (para o modo --wav esperar)
        self._reply: dict | None = None
        self._speaking: dict | None = None  # resposta tocando, de response_start até o playback_end
        self._confirming: int | None = None  # barge-in à espera do servidor (número da resposta)
        self._finish_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    async def send_loop(self) -> None:
        async for item in self.source.frames():
            if self.barge is not None:
                mic, ref, captured_at = item
                active = self._speaking is not None and self._confirming is None
                pcm, captured_at, fired = self.barge.process(mic, ref, captured_at, active)
                if fired is not None:
                    await self._start_barge_in(fired)  # já envia este bloco, junto com o que veio antes
                    continue
            else:
                pcm, captured_at = item
            if not self.can_send.is_set():
                if not self.source.finite:
                    continue  # microfone: descarta o que foi dito durante a resposta
                await self.can_send.wait()  # arquivo: pausa para não perder pedaços
                captured_at = time.monotonic()
            await self.ws.send(pcm)
            self.captured_at.append(captured_at)
            if self.barge is not None:
                self.barge.sent()

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
            command = msg.get("command")
            if msg.get("addressed", True):
                note = "  (ok, já estou quieta)" if command == "stop" else ""
                print(f"  #{msg['segment']} você: {msg['text'] or '(nada reconhecido)'}  "
                      f"[stt {t['stt_ms']} ms]{note}", flush=True)
            else:  # não chamou a Agatha (wake word): ela não vai responder
                print(f"  · ignorado (sem \"Agatha\"): {msg['text']}", flush=True)
            if not msg["text"] or not msg.get("addressed", True) or command == "stop":
                self.pending.discard(msg["segment"])
        elif kind == "response_start":
            self.can_send.clear()
            self.player.begin()
            self._reply = {"response": msg["response"], "segment": msg["segment"]}
            self._speaking = self._reply
            if self.barge is not None:
                self.barge.begin_stats(time.monotonic())
            print("  agatha: ", end="", flush=True)
        elif kind == "response_text":
            print(msg["text"], end=" ", flush=True)
        elif kind == "audio_end":
            self.player.end()
        elif kind == "response_end":
            self._finish_task = self._spawn(self._finish_reply(msg))
        elif kind == "barge_in_end":
            self._end_barge_in(msg)
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
        self._print_barge_stats()
        if self.wake.get("enabled"):
            print(f"    (pode continuar sem dizer \"Agatha\" por {self.wake['conversation_s']:g} s)", flush=True)
        print(flush=True)
        self.pending.discard(reply["segment"])
        self._speaking = None
        self.can_send.set()

    # --- Barge-in -----------------------------------------------------------------------

    async def _start_barge_in(self, score: float) -> None:
        """Ouviu "Agatha" por cima da resposta: pausa, avisa o servidor e manda o áudio desde antes
        do nome; o microfone segue aberto até o servidor decidir (`barge_in_end`)."""
        n = self._speaking["response"]
        self.player.pause()
        self._confirming = n
        await self.ws.send(json.dumps({"type": "barge_in", "response": n, "played_ms": round(self.player.played_ms())}))
        for pcm, captured_at in self.barge.pre_roll():
            await self.ws.send(pcm)
            self.captured_at.append(captured_at)
        self.can_send.set()
        print(f"\n  ✋ ouvi \"Agatha\" ({score:.2f}): pausando...", flush=True)

    def _end_barge_in(self, msg: dict) -> None:
        if self._confirming != msg["response"]:
            return
        self._confirming = None
        if msg["action"] == "resume":
            print("  (não era comigo; continuando)", flush=True)
            self.barge.hold(time.monotonic())
            if self._speaking is None:
                # ela já tinha terminado: o playback_end saiu durante a confirmação e o servidor o
                # ignorou; sem reenviar, ele só voltaria a ouvir no prazo de segurança
                self._spawn(self.ws.send(json.dumps({"type": "playback_end", "response": msg["response"],
                                                     "e2e_ms": None})))
                return
            self.can_send.clear()
            self.player.resume()
            print("  agatha: ", end="", flush=True)
            return
        # "stop": descarta o resto; se veio pergunta nova, o response_start dela chega em seguida
        print("  ■ interrompida", flush=True)
        self._print_barge_stats()
        if self._finish_task is not None:
            self._finish_task.cancel()  # sem playback_end: o servidor já encerrou essa resposta
        self.player.stop()
        if self._speaking is not None:
            self.pending.discard(self._speaking["segment"])
        self._reply = self._speaking = None
        self.can_send.set()

    def _print_barge_stats(self) -> None:
        line = self.barge.end_stats() if self.barge is not None else None
        if line:
            print(f"    ⓘ barge-in: {line}", flush=True)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def busy(self) -> bool:
        return bool(self.pending) or not self.can_send.is_set()


async def session(url: str, token: str, source: MicSource | WavSource | DuplexAudio, args, detector=None) -> None:
    duplex = isinstance(source, DuplexAudio)
    async with connect(url, open_timeout=5, max_size=2**22) as ws:
        hello = {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "client": f"agatha-client/{__version__} ({sys.platform})",
            "token": token,
            "audio": AUDIO_FORMAT,
        }
        if duplex:  # a resposta toca no mesmo stream do microfone, a 16 kHz (o servidor reamostra)
            hello["output_audio"] = {"sample_rate": source.sample_rate}
        await ws.send(json.dumps(hello))
        reply = json.loads(await ws.recv())
        if reply["type"] == "error":
            message = f"servidor recusou a conexão: {reply['code']}: {reply['message']}"
            if reply["code"] in FATAL_ERRORS:
                raise FatalError(message)
            raise ConnectionError(message)
        rate = reply["output_audio"]["sample_rate"]
        barge = None
        if duplex:
            if rate != source.sample_rate or not (reply.get("barge_in") or {}).get("enabled"):
                raise FatalError("o servidor não tem barge-in (atualize o servidor ou use --no-barge-in)")
            player = source
            detector.reset()
            barge = BargeIn(detector, source.sample_rate, threshold=args.barge_threshold,
                            delay_ms=args.echo_delay_ms, on_delay=report_delay)
            print(f"conectado a {url} (sessão {reply['session']}, resposta a {rate} Hz, barge-in ligado: "
                  f"diga \"Agatha\" enquanto ela fala para interromper)")
        else:
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

        s = Session(ws, source, player, wake, barge)
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


def report_delay(delay_ms: float, confidence: float | None) -> None:
    if confidence is None:
        print(f"\n  (barge-in: sem eco audível; detector ouvindo direto, atraso {delay_ms:.0f} ms)", flush=True)
    else:
        print(f"\n  (barge-in: eco medido, atraso {delay_ms:.0f} ms, confiança {confidence:.0f})", flush=True)


def load_detector(args):
    """O detector do barge-in, ou None (com aviso) se faltar o extra [barge-in] ou os modelos."""
    try:
        from .wakeword import WakeDetector

        return WakeDetector(args.wake_model)
    except (ImportError, FileNotFoundError) as e:
        print(f"barge-in desligado: {e}\n"
              "  instale com: pip install -e \".[barge-in]\" e copie os modelos (veja o README)\n")
        return None


async def run(url: str, token: str, source: MicSource | WavSource | DuplexAudio, args, detector=None) -> None:
    while True:
        try:
            await session(url, token, source, args, detector)
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
    parser.add_argument("--no-barge-in", action="store_true",
                        help="não ouve durante a resposta (microfone e saída em streams separados)")
    parser.add_argument("--wake-model", default="models",
                        help="pasta com agatha.onnx, melspectrogram.onnx e embedding_model.onnx (padrão: models)")
    parser.add_argument("--barge-threshold", type=float, default=0.6,
                        help="nota mínima do detector para interromper (padrão: 0.6; mais alto erra menos e acerta menos)")
    parser.add_argument("--echo-delay-ms", type=float,
                        help="atraso do eco em ms (padrão: medido nos primeiros segundos de fala dela)")
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
        detector = None
        if args.wav:
            source = WavSource(args.wav)
        elif not args.no_barge_in and (detector := load_detector(args)) is not None:
            source = DuplexAudio(args.device, args.output_device)
        else:
            source = MicSource(args.device)
        asyncio.run(run(url, token, source, args, detector))
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
