"""Servidor WebSocket: ouve, transcreve e responde em voz (protocolo v2, half-duplex).

Cada conexão é uma `Session` com dois estados (ADR-006):
- ouvindo: o áudio passa pelo VAD; frases completas são transcritas;
- respondendo: uma transcrição com texto dispara a resposta (LLM → TTS) e o microfone é
  ignorado até o cliente enviar `playback_end` (ou até o prazo de segurança).

Se a transcrição parecer cortada no meio (`looks_incomplete`), ela é segurada por
`continuation_ms`: se a pessoa voltar a falar, as partes são juntadas numa pergunta só.

Wake word (ADR-008): só responde a falas que chamam "Agatha" pelo nome, ou que começam dentro do
"modo conversa" (alguns segundos depois de cada resposta).
"""

import asyncio
import logging
import secrets
import signal
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import numpy as np
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import protocol
from .config import ServerConfig
from .conversation import Audio, Conversation, Done, TextChunk
from .prompts import system_message
from .text import looks_incomplete
from .wake import is_addressed
from .vad import Segmenter, SileroVAD, SpeechEnd, SpeechStart

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
PLAYBACK_GRACE_S = 15.0  # prazo extra, além da duração do áudio, para o cliente mandar playback_end


class SupportsTranscribe(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


class SupportsRespond(Protocol):
    def respond(self, user_text: str, t_ref: float) -> AsyncIterator[TextChunk | Audio | Done]: ...


class AgathaServer:
    def __init__(
        self,
        config: ServerConfig,
        transcriber: SupportsTranscribe,
        conversation_factory: Callable[[], SupportsRespond],
        output_sample_rate: int,
        vad_factory: Callable[[], Callable[[np.ndarray], float]] = SileroVAD,
        hello_timeout: float = HELLO_TIMEOUT_S,
        playback_grace_s: float = PLAYBACK_GRACE_S,
    ):
        self.config = config
        self.conversation_factory = conversation_factory
        self.output_sample_rate = output_sample_rate
        self.vad_factory = vad_factory
        self.playback_grace_s = playback_grace_s
        self._transcriber = transcriber
        self._hello_timeout = hello_timeout
        # Um único worker: a GPU transcreve uma frase por vez, em ordem de chegada.
        self._stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")

    async def handler(self, ws: ServerConnection) -> None:
        session_id = secrets.token_hex(4)
        peer = ws.remote_address[0] if ws.remote_address else "?"
        try:
            hello = await self._handshake(ws, session_id)
        except ConnectionClosed:
            return
        if hello is None:
            return
        log.info("[%s] conectado: %s (%s)", session_id, peer, hello.get("client", "cliente sem nome"))
        await Session(self, ws, session_id).run()

    async def transcribe(self, audio: np.ndarray) -> tuple[float, str, float]:
        """Transcreve na thread do STT; devolve (início, texto, fim) no relógio monotônico."""

        def job() -> tuple[float, str, float]:
            started = time.monotonic()
            text = self._transcriber.transcribe(audio)
            return started, text, time.monotonic()

        return await asyncio.get_running_loop().run_in_executor(self._stt_executor, job)

    async def _handshake(self, ws: ServerConnection, session_id: str) -> dict | None:
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
        await ws.send(protocol.ready(session_id, self.config.vad, self.output_sample_rate, self.config.wake))
        return hello

    async def _reject(self, ws: ServerConnection, e: protocol.ProtocolError) -> None:
        log.warning("conexão recusada (%s): %s", e.code, e.message)
        await ws.send(protocol.error(e.code, e.message))
        await ws.close(e.close_code, e.code)

    def shutdown(self) -> None:
        self._stt_executor.shutdown(wait=False, cancel_futures=True)


class Session:
    LISTENING = "ouvindo"
    RESPONDING = "respondendo"

    def __init__(self, server: AgathaServer, ws: ServerConnection, session_id: str):
        self.server = server
        self.ws = ws
        self.id = session_id
        self.segmenter = Segmenter(server.config.vad, server.vad_factory())
        self.conversation = server.conversation_factory()
        self.state = self.LISTENING
        self.response_count = 0
        self.current_response: int | None = None
        self._response_task: asyncio.Task | None = None
        self._resume_timer: asyncio.TimerHandle | None = None
        # transcrição que parece incompleta, à espera de continuação: (evento, texto, fim do STT)
        self._held: tuple[SpeechEnd, str, float] | None = None
        self._hold_timer: asyncio.TimerHandle | None = None
        self._speaking = False  # há uma frase confirmada em andamento (entre vad start e end)
        self._awake_until = 0.0  # fim do "modo conversa" (relógio monotônico)
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    await self._on_text(message)
                else:
                    await self._on_audio(message)
        except ConnectionClosed:
            pass
        finally:
            for timer in (self._resume_timer, self._hold_timer):
                if timer:
                    timer.cancel()
            for task in self._tasks:
                task.cancel()
            log.info("[%s] desconectado (código %s)", self.id, self.ws.close_code)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _send(self, message: str | bytes) -> None:
        try:
            await self.ws.send(message)
        except ConnectionClosed:
            pass

    # --- Microfone --------------------------------------------------------------------

    async def _on_audio(self, data: bytes) -> None:
        try:
            samples = protocol.pcm16_to_float32(data)
        except protocol.ProtocolError as e:
            await self._send(protocol.error(e.code, e.message))
            return
        if self.state != self.LISTENING:
            self.segmenter.discard(len(samples))  # half-duplex: conta a posição, mas não ouve
            return
        for event in self.segmenter.feed(samples):
            await self._send(protocol.vad_event(event))
            if isinstance(event, SpeechStart):
                log.debug("[%s] #%d início da fala (%d ms)", self.id, event.segment, event.audio_ms)
                self._speaking = True
                if self._hold_timer:  # voltou a falar: a frase segurada vai ganhar continuação
                    self._hold_timer.cancel()
                    self._hold_timer = None
            elif isinstance(event, SpeechEnd):
                self._speaking = False
                self._spawn(self._transcribe(event))

    async def _transcribe(self, event: SpeechEnd) -> None:
        submitted = time.monotonic()
        started, text, finished = await self.server.transcribe(event.audio)
        timings = {
            "speech_ms": event.speech_ms,
            "endpoint_ms": round(event.endpoint_ms),
            "queue_ms": round((started - submitted) * 1000),
            "stt_ms": round((finished - started) * 1000),
            "total_ms": round((finished - event.last_speech_at) * 1000),
        }
        log.info(
            "[%s] #%d %r | fala %.1f s · endpoint %d ms · stt %d ms · total %d ms",
            self.id, event.segment, text, event.speech_ms / 1000,
            timings["endpoint_ms"], timings["stt_ms"], timings["total_ms"],
        )
        addressed = self._addressed(event, text)
        await self._send(protocol.transcript(event.segment, text, timings, addressed))
        if self.state != self.LISTENING:
            if text:
                log.info("[%s] #%d ignorada: resposta em andamento (half-duplex)", self.id, event.segment)
            return
        if not text:
            if self._held and self._hold_timer is None:  # a "continuação" era só ruído
                self._release_held()
            return
        if not addressed:
            log.info("[%s] #%d ignorada: não chamou a Agatha", self.id, event.segment)
            return
        if self._held:
            text = f"{self._held[1]} {text}"
            self._held = None
            if self._hold_timer:
                self._hold_timer.cancel()
                self._hold_timer = None
        continuation = self.server.config.vad.continuation_ms
        if continuation > 0 and looks_incomplete(text):
            log.info("[%s] #%d parece incompleta; aguardando continuação: %r", self.id, event.segment, text)
            self._held = (event, text, finished)
            # Se a pessoa já voltou a falar (antes de esta transcrição ficar pronta), espera a
            # próxima transcrição em vez de um prazo fixo.
            if not self._speaking:
                self._hold_timer = asyncio.get_running_loop().call_later(continuation / 1000, self._release_held)
            return
        self._start_response(event, text, finished)

    def _addressed(self, event: SpeechEnd, text: str) -> bool:
        """A fala é para a Agatha? Nome no começo/fim, modo conversa ou continuação de uma que era."""
        wake = self.server.config.wake
        if not wake.enabled or self._held is not None:
            return True
        started_at = event.last_speech_at - event.speech_ms / 1000
        return started_at <= self._awake_until or is_addressed(text, wake)

    def _release_held(self) -> None:
        self._hold_timer = None
        if self._held and self.state == self.LISTENING:
            event, text, finished = self._held
            self._held = None
            self._start_response(event, text, finished)

    # --- Resposta ---------------------------------------------------------------------

    def _start_response(self, event: SpeechEnd, text: str, stt_done_at: float) -> None:
        self.state = self.RESPONDING
        self.segmenter.reset()  # descarta o que estava sendo ouvido; a posição continua contando
        self.response_count += 1
        self.current_response = self.response_count
        self._response_task = self._spawn(self._respond(self.response_count, event, text, stt_done_at))

    async def _respond(self, n: int, event: SpeechEnd, text: str, stt_done_at: float) -> None:
        t_ref = event.last_speech_at
        rate = self.server.output_sample_rate
        await self._send(protocol.response_start(n, event.segment))
        audio_bytes = 0
        first_audio_at = None
        done = Done("error", "")
        try:
            async for item in self.conversation.respond(text, t_ref):
                if isinstance(item, TextChunk):
                    await self._send(protocol.response_text(n, item.text))
                elif isinstance(item, Audio):
                    if audio_bytes == 0:
                        await self._send(protocol.audio_start(n, rate))
                    for frame in protocol.audio_frames(item.pcm):
                        await self._send(frame)
                        if first_audio_at is None:
                            first_audio_at = time.monotonic()
                    audio_bytes += len(item.pcm)
                elif isinstance(item, Done):
                    done = item
        except Exception:
            log.exception("[%s] resposta %d falhou", self.id, n)
        if audio_bytes:
            await self._send(protocol.audio_end(n))

        audio_s = audio_bytes / 2 / rate
        timings = {
            "stt_ms": round((stt_done_at - t_ref) * 1000),
            **done.timings,
            "first_audio_ms": round((first_audio_at - t_ref) * 1000) if first_audio_at else None,
            "audio_ms": round(audio_s * 1000),
        }
        await self._send(protocol.response_end(n, done.reason, done.text, timings))
        log.info("[%s] resposta %d (%s) %r | %s", self.id, n, done.reason, done.text,
                 " · ".join(f"{k.removesuffix('_ms')} {v} ms" for k, v in timings.items() if v is not None))
        # prazo de segurança: se o playback_end não chegar, volta a ouvir sozinho
        self._resume_timer = asyncio.get_running_loop().call_later(
            audio_s + self.server.playback_grace_s, self._resume_after_timeout, n
        )

    async def _on_text(self, raw: str) -> None:
        try:
            msg = protocol.parse_message(raw)
            if msg["type"] != "playback_end":
                raise protocol.ProtocolError("bad_message", f"tipo de mensagem inesperado: {msg['type']!r}")
            n, e2e_ms = protocol.parse_playback_end(msg)
        except protocol.ProtocolError as e:
            await self._send(protocol.error(e.code, e.message))
            return
        self._on_playback_end(n, e2e_ms)

    def _on_playback_end(self, n: int, e2e_ms: float | None) -> None:
        if self.state != self.RESPONDING or n != self.current_response:
            log.debug("[%s] playback_end %d ignorado (estado %s, resposta atual %s)",
                      self.id, n, self.state, self.current_response)
            return
        if self._response_task and not self._response_task.done():
            log.warning("[%s] playback_end %d antes do fim da resposta; ignorado", self.id, n)
            return
        e2e = f"{e2e_ms:.0f} ms" if e2e_ms is not None else "n/d"
        log.info("[%s] resposta %d tocada · fim da fala → primeiro som (cliente): %s", self.id, n, e2e)
        self._resume()

    def _resume_after_timeout(self, n: int) -> None:
        if self.state == self.RESPONDING and self.current_response == n:
            log.warning("[%s] playback_end %d não chegou; voltando a ouvir", self.id, n)
            self._resume()

    def _resume(self) -> None:
        if self._resume_timer:
            self._resume_timer.cancel()
            self._resume_timer = None
        self.segmenter.reset()
        self.state = self.LISTENING
        self.current_response = None
        # modo conversa: por alguns segundos, não precisa chamar pelo nome
        self._awake_until = time.monotonic() + self.server.config.wake.conversation_s


async def run(config: ServerConfig, transcriber: SupportsTranscribe, llm, synthesizer) -> None:
    tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
    server = AgathaServer(
        config,
        transcriber,
        conversation_factory=lambda: Conversation(llm, synthesizer, tts_executor, config.conversation),
        output_sample_rate=synthesizer.sample_rate,
    )
    if await llm.healthy():
        log.info("LLM disponível em %s", config.llm.url)
        # Deixa o prompt de sistema no cache do llama.cpp: sem isso, a 1ª pergunta depois de
        # subir o LLM paga ~1,5 s processando o prompt inteiro.
        t0 = time.monotonic()
        try:
            await llm.prefill([system_message()])
            log.info("prompt de sistema no cache do LLM (%.0f ms)", (time.monotonic() - t0) * 1000)
        except Exception as e:
            log.warning("falha ao pré-carregar o prompt no LLM: %s", e)
    else:
        log.warning("LLM indisponível em %s: as respostas vão falhar até ele subir "
                    "(deploy/up.sh)", config.llm.url)
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
        tts_executor.shutdown(wait=False, cancel_futures=True)
        await llm.aclose()
