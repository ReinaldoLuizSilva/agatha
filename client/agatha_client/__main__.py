"""Cliente da Agatha: conversa por voz com o servidor.

Uso:
    python -m agatha_client                       # microfone e saída de som padrão
    python -m agatha_client --no-barge-in         # sem interromper a Agatha (half-duplex puro)
    pythonw -m agatha_client --tray               # em segundo plano, com ícone na bandeja (Windows)
    python -m agatha_client --autostart ligar     # inicia com o Windows, em segundo plano
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
from .controls import CONNECTING, LISTENING, OFFLINE, SPEAKING, Controls

PROTOCOL_VERSION = 2
FRAME_MS = round(FRAME_S * 1000)  # 32
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1}
DEFAULT_URL = "ws://192.168.1.25:8765/"
RETRY_S = 3.0
# Erros em que tentar de novo não adianta
FATAL_ERRORS = {"unauthorized", "unsupported_protocol", "unsupported_audio"}


class Session:
    def __init__(self, ws, source: MicSource | WavSource | DuplexAudio, player: Player | NullPlayer | DuplexAudio,
                 wake: dict | None = None, barge: BargeIn | None = None, controls: Controls | None = None):
        self.ws = ws
        self.controls = controls or Controls()
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
        self._barge_fire: dict | None = None  # disparo em confirmação: quando foi e se já mediu o atraso
        self.barge_counts = {"disparos": 0, "parou": 0, "retomou": 0}

    async def send_loop(self) -> None:
        async for item in self.source.frames():
            if self.barge is not None:
                mic, ref, captured_at = item
                active = (self._speaking is not None and self._confirming is None and self.controls.barge_in
                          and not self.controls.muted)
                pcm, captured_at, fired = self.barge.process(mic, ref, captured_at, active)
                if fired is not None:
                    await self._start_barge_in(fired, captured_at)  # já envia este bloco e o que veio antes
                    continue
            else:
                pcm, captured_at = item
            if self.controls.muted:
                pcm = bytes(len(pcm))  # silêncio: fecha a frase que estiver aberta e mantém a contagem
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
                self._report_barge_delay(msg["audio_ms"])
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
            self.controls.status(SPEAKING)
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
        self.controls.status(LISTENING)

    # --- Barge-in -----------------------------------------------------------------------

    async def _start_barge_in(self, score: float, fired_at: float = 0.0) -> None:
        """Ouviu "Agatha" por cima da resposta: pausa, avisa o servidor e manda o áudio desde antes
        do nome; o microfone segue aberto até o servidor decidir (`barge_in_end`)."""
        n = self._speaking["response"]
        self.player.pause()
        self._confirming = n
        self._barge_fire = {"at": fired_at, "reported": False}
        self.barge_counts["disparos"] += 1
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
        self.barge_counts["retomou" if msg["action"] == "resume" else "parou"] += 1
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
        self.controls.status(LISTENING)

    def _report_barge_delay(self, audio_ms: int) -> None:
        """No 1º começo de fala depois de um disparo: quanto tempo depois do começo da sua fala o
        detector disparou (o nome dura ~0,5 s; meta: pausa < 300 ms depois do fim dele) e quanto a
        pausa leva para sair nas caixas (a latência de saída)."""
        fire = self._barge_fire
        if self._confirming is None or fire is None or fire["reported"]:
            return
        fire["reported"] = True
        index = audio_ms // FRAME_MS
        if 0 <= index < len(self.captured_at) and fire["at"]:
            delay = round((fire["at"] - self.captured_at[index]) * 1000)
            out = round(getattr(self.player, "latency", 0) * 1000)
            print(f"    ⏱ barge-in: disparou {delay} ms depois do começo da sua fala; a pausa sai nas caixas "
                  f"~{out} ms depois", flush=True)

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


async def session(url: str, token: str, source: MicSource | WavSource | DuplexAudio, args, detector=None,
                  controls: Controls | None = None) -> None:
    controls = controls or Controls()
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
                            delay_ms=args.echo_delay_ms, on_delay=report_delay,
                            debug=(lambda line: print(line, flush=True)) if args.barge_debug else None)
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

        s = Session(ws, source, player, wake, barge, controls)
        controls.status(LISTENING)
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
            c = s.barge_counts
            if c["disparos"]:
                print(f"barge-in nesta conexão: {c['disparos']} disparos, {c['parou']} confirmados (ela parou), "
                      f"{c['retomou']} à toa (ela continuou)", flush=True)


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


async def run(url: str, token: str, source: MicSource | WavSource | DuplexAudio, args, detector=None,
              controls: Controls | None = None) -> None:
    controls = controls or Controls()
    while True:
        controls.status(CONNECTING)
        try:
            await session(url, token, source, args, detector, controls)
            if source.finite:
                return
        except (ConnectionClosed, ConnectionError, OSError, TimeoutError) as e:
            if source.finite:
                raise FatalError(f"conexão falhou: {e}")
            controls.status(OFFLINE)
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
    parser.add_argument("--barge-debug", action="store_true",
                        help="durante a fala dela, mostra a cada 0,5 s o nível do microfone e a nota do detector")
    parser.add_argument("--duplex-latency", choices=["low", "high"], default="low",
                        help="latência do stream do barge-in (padrão: low; high é a do gravador)")
    parser.add_argument("--tray", action="store_true",
                        help="em segundo plano, com ícone na bandeja; o que sairia na tela vai para o log "
                             "(%%LOCALAPPDATA%%\\Agatha\\client.log). Use com pythonw (extra [tray])")
    parser.add_argument("--autostart", choices=["ligar", "desligar"],
                        help="inicia (ou não) a Agatha em segundo plano junto com o Windows e sai")
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
    if args.autostart:
        return autostart(args.autostart)
    if args.tray:
        return tray_main(args)

    url, token = server_address(args)
    if not token:
        print("token não informado (--token, AGATHA_TOKEN ou .env)", file=sys.stderr)
        return 2

    try:
        source, detector = open_audio(args)
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


def server_address(args) -> tuple[str, str | None]:
    env = {**read_env_file(Path(".env")), **os.environ}
    return args.url or env.get("AGATHA_URL") or DEFAULT_URL, args.token or env.get("AGATHA_TOKEN")


def open_audio(args):
    detector = None
    if args.wav:
        return WavSource(args.wav), None
    if not args.no_barge_in and (detector := load_detector(args)) is not None:
        return DuplexAudio(args.device, args.output_device, latency=args.duplex_latency), detector
    return MicSource(args.device), None


def autostart(action: str) -> int:
    from . import autostart as auto

    try:
        if action == "ligar":
            print(f"pronto: a Agatha vai iniciar com o Windows, em segundo plano ({auto.enable()})")
        else:
            print("pronto: não inicia mais com o Windows" if auto.disable() else "já não iniciava com o Windows")
    except (RuntimeError, OSError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return 0


def tray_main(args) -> int:
    """Modo bandeja (ADR-021): sem terminal; tudo o que seria impresso vai para o log."""
    from .instance import SingleInstance
    from .tray import TrayApp, log_dir, setup_logging

    log_path = setup_logging()
    instance = SingleInstance(lock_dir=log_dir())
    if not instance.acquire():
        print("a Agatha já está rodando (veja o ícone na bandeja); saindo")
        return 1
    print(f"Agatha {__version__} em segundo plano; log em {log_path}")
    url, token = server_address(args)
    if not token:
        print("token não informado (AGATHA_TOKEN no .env da pasta do cliente)", file=sys.stderr)
        return 2
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except ImportError as e:
        print(f"modo bandeja precisa do extra [tray]: pip install -e \".[tray]\" ({e})", file=sys.stderr)
        return 1
    try:
        source, detector = open_audio(args)
        client = lambda controls: run(url, token, source, args, detector, controls)  # noqa: E731
    except FatalError as error:  # ex.: microfone que não abre: a bandeja fica vermelha e avisa
        detector = None

        async def client(controls, error=error):
            raise error
    try:
        TrayApp(client, has_barge_in=detector is not None, log_path=log_path).run()
    finally:
        instance.release()
    print("até logo!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
