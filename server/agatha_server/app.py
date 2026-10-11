"""Servidor WebSocket: recebe áudio, segmenta frases com o VAD e devolve transcrições."""

import asyncio
import logging
import secrets
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Protocol

import numpy as np
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import protocol
from .config import ServerConfig
from .vad import Segmenter, SileroVAD, SpeechEnd, SpeechStart

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0


class SupportsTranscribe(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


class AgathaServer:
    def __init__(
        self,
        config: ServerConfig,
        transcriber: SupportsTranscribe,
        vad_factory: Callable[[], Callable[[np.ndarray], float]] = SileroVAD,
        hello_timeout: float = HELLO_TIMEOUT_S,
    ):
        self.config = config
        self._transcriber = transcriber
        self._vad_factory = vad_factory
        self._hello_timeout = hello_timeout
        # Um único worker: a GPU transcreve uma frase por vez, em ordem de chegada.
        self._stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")

    async def handler(self, ws: ServerConnection) -> None:
        session = secrets.token_hex(4)
        peer = ws.remote_address[0] if ws.remote_address else "?"
        try:
            hello = await self._handshake(ws, session)
        except ConnectionClosed:
            return
        if hello is None:
            return
        log.info("[%s] conectado: %s (%s)", session, peer, hello.get("client", "cliente sem nome"))

        segmenter = Segmenter(self.config.vad, self._vad_factory())
        pending: set[asyncio.Task] = set()
        try:
            async for message in ws:
                if isinstance(message, str):
                    await self._on_text(ws, message)
                    continue
                try:
                    samples = protocol.pcm16_to_float32(message)
                except protocol.ProtocolError as e:
                    await ws.send(protocol.error(e.code, e.message))
                    continue
                for event in segmenter.feed(samples):
                    await ws.send(protocol.vad_event(event))
                    if isinstance(event, SpeechStart):
                        log.debug("[%s] #%d início da fala (%d ms)", session, event.segment, event.audio_ms)
                    elif isinstance(event, SpeechEnd):
                        task = asyncio.create_task(self._transcribe(ws, session, event))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
        except ConnectionClosed:
            pass
        finally:
            for task in pending:
                task.cancel()
            log.info("[%s] desconectado", session)

    async def _handshake(self, ws: ServerConnection, session: str) -> dict | None:
        try:
            raw = await asyncio.wait_for(ws.recv(), self._hello_timeout)
            hello = protocol.parse_hello(raw, self.config.token)
        except TimeoutError:
            await self._reject(ws, protocol.ProtocolError(
                "hello_timeout", f"nenhum 'hello' em {self._hello_timeout:g} s", protocol.CLOSE_POLICY_VIOLATION
            ))
            return None
        except protocol.ProtocolError as e:
            await self._reject(ws, e)
            return None
        await ws.send(protocol.ready(session, self.config.vad))
        return hello

    async def _reject(self, ws: ServerConnection, e: protocol.ProtocolError) -> None:
        log.warning("conexão recusada (%s): %s", e.code, e.message)
        await ws.send(protocol.error(e.code, e.message))
        await ws.close(e.close_code, e.code)

    async def _on_text(self, ws: ServerConnection, raw: str) -> None:
        # Na versão 1 o cliente não envia texto depois do hello.
        try:
            msg = protocol.parse_message(raw)
        except protocol.ProtocolError as e:
            await ws.send(protocol.error(e.code, e.message))
            return
        await ws.send(protocol.error("bad_message", f"tipo de mensagem inesperado: {msg['type']!r}"))

    async def _transcribe(self, ws: ServerConnection, session: str, event: SpeechEnd) -> None:
        submitted = time.monotonic()

        def job() -> tuple[float, str, float]:
            started = time.monotonic()
            text = self._transcriber.transcribe(event.audio)
            return started, text, time.monotonic()

        loop = asyncio.get_running_loop()
        started, text, finished = await loop.run_in_executor(self._stt_executor, job)
        timings = {
            "speech_ms": event.speech_ms,
            "endpoint_ms": round(event.endpoint_ms),
            "queue_ms": round((started - submitted) * 1000),
            "stt_ms": round((finished - started) * 1000),
            "total_ms": round((finished - event.last_speech_at) * 1000),
        }
        log.info(
            "[%s] #%d %r | fala %.1f s · endpoint %d ms · stt %d ms · total %d ms",
            session, event.segment, text, event.speech_ms / 1000,
            timings["endpoint_ms"], timings["stt_ms"], timings["total_ms"],
        )
        try:
            await ws.send(protocol.transcript(event.segment, text, timings))
        except ConnectionClosed:
            pass

    def shutdown(self) -> None:
        self._stt_executor.shutdown(wait=False, cancel_futures=True)


async def run(config: ServerConfig, transcriber: SupportsTranscribe) -> None:
    server = AgathaServer(config, transcriber)
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
    try:
        async with serve(server.handler, config.host, config.port):
            log.info("ouvindo em ws://%s:%d/", config.host, config.port)
            await stop
            log.info("encerrando")
    finally:
        server.shutdown()
