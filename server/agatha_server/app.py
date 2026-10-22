"""Servidor WebSocket: ouve, transcreve e responde em voz (protocolo v2, half-duplex).

Cada conexão é uma `Session` com dois estados (ADR-006):
- ouvindo: o áudio passa pelo VAD; frases completas são transcritas;
- respondendo: uma transcrição com texto dispara a resposta (LLM → TTS) e o microfone é
  ignorado até o cliente enviar `playback_end` (ou até o prazo de segurança).

Se a transcrição parecer cortada no meio (`looks_incomplete`), ela é segurada por
`continuation_ms`: se a pessoa voltar a falar, as partes são juntadas numa pergunta só.

Wake word (ADR-008): só responde a falas que chamam "Agatha" pelo nome, ou que começam dentro do
"modo conversa" (alguns segundos depois de cada resposta).

Barge-in (ADR-016): enquanto ela fala, o cliente pode avisar que ouviu "Agatha" (`barge_in`). A
sessão passa a "confirmando": volta a ouvir sem cancelar nada, e a primeira frase transcrita decide.
Se chamou a Agatha (ou mandou parar), a resposta é cancelada, o histórico fica só com o que foi
ouvido e o cliente recebe `barge_in_end` "stop"; se era pergunta nova, a resposta a ela começa em
seguida. Se não era com ela (ou o prazo acabou sem fala), "resume": o cliente volta a tocar.
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
from .audio import resample
from .config import ServerConfig
from .conversation import Audio, Conversation, Done, TextChunk, build_prefix
from .memory import MemoryStore
from .tools import ToolRegistry
from .tools.calc import calculator
from .tools.github import GitHubClient, github_tools
from .tools.news import NewsClient, news_tools
from .tools.weather import WeatherClient, weather_tools
from .tools.memory import memory_tools
from .tools.oracle import OracleClient, oracle_tools
from .tools.zabbix import ZabbixClient, zabbix_tools
from .text import looks_incomplete
from .turnlog import TurnLog
from .wake import from_name, interruption_kind, is_addressed, is_idle_stop
from .vad import Segmenter, SileroVAD, SpeechEnd, SpeechStart

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
PLAYBACK_GRACE_S = 15.0  # prazo extra, além da duração do áudio, para o cliente mandar playback_end
# depois de um barge_in, quanto esperar o começo de uma fala para confirmar; o cliente já manda o
# áudio desde antes do nome, então a fala começa logo (num falso disparo, ela fica calada só isso)
BARGE_IN_CONFIRM_S = 2.0


class SupportsTranscribe(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


class SupportsRespond(Protocol):
    def respond(self, user_text: str, t_ref: float) -> AsyncIterator[TextChunk | Audio | Done]: ...

    def interrupt(self, spoken_text: str) -> None: ...


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
        turn_log: TurnLog | None = None,
        barge_in_confirm_s: float = BARGE_IN_CONFIRM_S,
    ):
        self.config = config
        self.conversation_factory = conversation_factory
        self.output_sample_rate = output_sample_rate
        self.vad_factory = vad_factory
        self.playback_grace_s = playback_grace_s
        self.turn_log = turn_log
        self.barge_in_confirm_s = barge_in_confirm_s
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
        client = hello.get("client", "cliente sem nome")
        log.info("[%s] conectado: %s (%s)", session_id, peer, client)
        if self.turn_log is not None:
            self.turn_log.write("connect", session=session_id, client=client, peer=peer)
        rate = (hello.get("output_audio") or {}).get("sample_rate", self.output_sample_rate)
        await Session(self, ws, session_id, rate).run()

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
            rate = (hello.get("output_audio") or {}).get("sample_rate", self.output_sample_rate)
            if rate not in (self.output_sample_rate, *protocol.OUTPUT_RATES):
                raise protocol.ProtocolError(
                    "unsupported_audio",
                    f"taxa de saída {rate} não suportada (aceitas: {self.output_sample_rate}, "
                    f"{', '.join(map(str, protocol.OUTPUT_RATES))})", protocol.CLOSE_UNSUPPORTED_DATA)
        except TimeoutError:
            await self._reject(ws, protocol.ProtocolError(
                "hello_timeout", f"nenhum 'hello' em {self._hello_timeout:g} s", protocol.CLOSE_POLICY_VIOLATION
            ))
            return None
        except protocol.ProtocolError as e:
            await self._reject(ws, e)
            return None
        await ws.send(protocol.ready(session_id, self.config.vad, rate, self.config.wake, self.barge_in_confirm_s))
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
    CONFIRMING = "confirmando"  # barge-in: ouvindo de novo, com a resposta pausada no cliente

    def __init__(self, server: AgathaServer, ws: ServerConnection, session_id: str, output_rate: int | None = None):
        self.server = server
        self.ws = ws
        self.id = session_id
        self.output_rate = output_rate or server.output_sample_rate
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
        self._pending_turn: dict | None = None  # resposta à espera do playback_end para ir ao registro
        self._tasks: set[asyncio.Task] = set()
        # resposta atual: (texto do trecho, onde o áudio dele começa em ms) e a duração total
        self._chunks: list[tuple[str, float]] = []
        self._audio_ms = 0.0
        self._barge: dict | None = None  # barge-in em confirmação: {"response", "played_ms"}
        self._barge_timer: asyncio.TimerHandle | None = None
        self._transcribing: set[int] = set()  # frases no STT agora
        self._confirm_segments: set[int] = set()  # frases que começaram durante a confirmação atual
        self._dropped: set[int] = set()  # frases de uma confirmação que acabou em "resume"

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
            self._log_response(None, "disconnected")
            for timer in (self._resume_timer, self._hold_timer, self._barge_timer):
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
        if self.state == self.RESPONDING:
            self.segmenter.discard(len(samples))  # half-duplex: conta a posição, mas não ouve
            return
        for event in self.segmenter.feed(samples):
            await self._send(protocol.vad_event(event))
            if isinstance(event, SpeechStart):
                log.debug("[%s] #%d início da fala (%d ms)", self.id, event.segment, event.audio_ms)
                self._speaking = True
                if self.state == self.CONFIRMING:
                    self._confirm_segments.add(event.segment)
                if self._hold_timer:  # voltou a falar: a frase segurada vai ganhar continuação
                    self._hold_timer.cancel()
                    self._hold_timer = None
            elif isinstance(event, SpeechEnd):
                self._speaking = False
                self._transcribing.add(event.segment)
                self._spawn(self._transcribe(event))

    async def _transcribe(self, event: SpeechEnd) -> None:
        submitted = time.monotonic()
        try:
            started, text, finished = await self.server.transcribe(event.audio)
        except Exception:
            # Uma falha no STT (ex.: falta de VRAM) não pode sumir em silêncio nem derrubar a sessão:
            # registra e trata a fala como vazia.
            log.exception("[%s] #%d falha na transcrição", self.id, event.segment)
            started = finished = time.monotonic()
            text = ""
        finally:
            self._transcribing.discard(event.segment)
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
        idle_stop = (addressed and self.state == self.LISTENING and self._held is None
                     and event.segment not in self._dropped and is_idle_stop(text, self.server.config.wake))
        await self._send(protocol.transcript(event.segment, text, timings, addressed,
                                             "stop" if idle_stop else None))

        def record(outcome: str) -> None:
            self._log("transcript", segment=event.segment, text=text, addressed=addressed, outcome=outcome,
                      timings=timings)

        if event.segment in self._dropped:
            # dita durante uma confirmação que já acabou em "resume": não vira pergunta depois
            self._dropped.discard(event.segment)
            log.info("[%s] #%d ignorada: era da confirmação de um barge-in já retomado", self.id, event.segment)
            record("barge_in_resumed")
            return
        if self.state == self.CONFIRMING and self._barge is not None:
            self._confirm_segments.discard(event.segment)
            await self._decide_barge_in(event, text, finished, record)
            return
        if self.state != self.LISTENING:
            if text:
                log.info("[%s] #%d ignorada: resposta em andamento (half-duplex)", self.id, event.segment)
            record("busy")
            return
        if not text:
            record("empty")
            if self._held and self._hold_timer is None:  # a "continuação" era só ruído
                self._release_held()
            return
        if not addressed:
            log.info("[%s] #%d ignorada: não chamou a Agatha", self.id, event.segment)
            record("not_addressed")
            return
        if idle_stop:
            # "Agatha, para" com ela calada (o detector perdeu o barge-in e a frase chegou depois):
            # nada a parar, e o LLM só responderia "não entendi"
            log.info("[%s] #%d comando de parar sem resposta em andamento: nada a fazer", self.id, event.segment)
            record("stop_idle")
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
            record("held")
            return
        record("response")
        self._start_response(event, text, finished)

    def _addressed(self, event: SpeechEnd, text: str) -> bool:
        """A fala é para a Agatha? Nome no começo/fim, modo conversa ou continuação de uma que era.

        Num barge-in vale só o nome ou um comando de parar: o modo conversa não conta.
        """
        wake = self.server.config.wake
        if self.state == self.CONFIRMING:
            return bool(text) and (is_addressed(text, wake) or interruption_kind(text, wake) == "stop")
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
        self._chunks, self._audio_ms = [], 0.0
        self.response_count += 1
        self.current_response = self.response_count
        self._response_task = self._spawn(self._respond(self.response_count, event, text, stt_done_at))

    async def _respond(self, n: int, event: SpeechEnd, text: str, stt_done_at: float) -> None:
        t_ref = event.last_speech_at
        rate = self.output_rate
        native = self.server.output_sample_rate
        await self._send(protocol.response_start(n, event.segment))
        audio_bytes = 0
        first_audio_at = None
        done = Done("error", "")
        stream = self.conversation.respond(text, t_ref)
        try:
            async for item in stream:
                if isinstance(item, TextChunk):
                    self._chunks.append((item.text, audio_bytes / 2 / rate * 1000))
                    await self._send(protocol.response_text(n, item.text))
                elif isinstance(item, Audio):
                    pcm = resample(item.pcm, native, rate)
                    if audio_bytes == 0:
                        await self._send(protocol.audio_start(n, rate))
                    for frame in protocol.audio_frames(pcm):
                        await self._send(frame)
                        if first_audio_at is None:
                            first_audio_at = time.monotonic()
                    audio_bytes += len(pcm)
                    self._audio_ms = audio_bytes / 2 / rate * 1000
                elif isinstance(item, Done):
                    done = item
        except Exception:
            log.exception("[%s] resposta %d falhou", self.id, n)
        finally:
            # cancelada (barge-in): fecha o gerador já, para o llama.cpp parar de gerar
            await stream.aclose()
        if audio_bytes:
            await self._send(protocol.audio_end(n))

        audio_s = audio_bytes / 2 / rate
        timings = {
            "stt_ms": round((stt_done_at - t_ref) * 1000),
            **done.timings,
            "first_audio_ms": round((first_audio_at - t_ref) * 1000) if first_audio_at else None,
            "audio_ms": round(audio_s * 1000),
        }
        await self._send(protocol.response_end(n, done.reason, done.text, timings, done.tools))
        self._pending_turn = {"response": n, "segment": event.segment, "user_text": text, "reason": done.reason,
                              "text": done.text, "tools": done.tools, "timings": timings}
        tools = f" [ferramentas: {', '.join(done.tools)}]" if done.tools else ""
        log.info("[%s] resposta %d (%s)%s %r | %s", self.id, n, done.reason, tools, done.text,
                 " · ".join(f"{k.removesuffix('_ms')} {v} ms" for k, v in timings.items() if v is not None))
        # prazo de segurança: se o playback_end não chegar, volta a ouvir sozinho
        self._resume_timer = asyncio.get_running_loop().call_later(
            audio_s + self.server.playback_grace_s, self._resume_after_timeout, n
        )

    async def _on_text(self, raw: str) -> None:
        try:
            msg = protocol.parse_message(raw)
            if msg["type"] == "playback_end":
                self._on_playback_end(*protocol.parse_playback_end(msg))
            elif msg["type"] == "barge_in":
                await self._on_barge_in(*protocol.parse_barge_in(msg))
            else:
                raise protocol.ProtocolError("bad_message", f"tipo de mensagem inesperado: {msg['type']!r}")
        except protocol.ProtocolError as e:
            await self._send(protocol.error(e.code, e.message))

    # --- Barge-in ---------------------------------------------------------------------

    async def _on_barge_in(self, n: int, played_ms: float) -> None:
        if self.state != self.RESPONDING or n != self.current_response:
            log.info("[%s] barge_in %d sem resposta em andamento (estado %s); retomando", self.id, n, self.state)
            await self._send(protocol.barge_in_end(n, "resume"))
            return
        log.info("[%s] barge-in na resposta %d após %.0f ms de áudio; confirmando", self.id, n, played_ms)
        self.state = self.CONFIRMING
        self._barge = {"response": n, "played_ms": played_ms}
        self._confirm_segments = set()
        self.segmenter.reset()  # o cliente manda em seguida o áudio desde antes do nome
        self._arm_barge_timer(self.server.barge_in_confirm_s)

    def _arm_barge_timer(self, delay: float) -> None:
        if self._barge_timer:
            self._barge_timer.cancel()
        self._barge_timer = asyncio.get_running_loop().call_later(delay, self._barge_timeout)

    def _barge_timeout(self) -> None:
        self._barge_timer = None
        if self.state != self.CONFIRMING:
            return
        if self._speaking or self._transcribing & self._confirm_segments:
            # ainda falando, ou a frase ainda está no STT (que às vezes leva ~2 s): espera ela decidir
            self._arm_barge_timer(0.5)
            return
        log.info("[%s] barge-in sem fala para confirmar; retomando", self.id)
        self._log("barge_in", response=self._barge["response"], played_ms=round(self._barge["played_ms"]),
                  outcome="timeout")
        self._spawn(self._end_barge_in("resume"))

    async def _decide_barge_in(self, event: SpeechEnd, text: str, finished: float, record) -> None:
        barge, wake = self._barge, self.server.config.wake
        n, played_ms = barge["response"], barge["played_ms"]
        if not (text and (is_addressed(text, wake) or interruption_kind(text, wake) == "stop")):
            log.info("[%s] #%d não era com a Agatha (%r); retomando a resposta %d", self.id, event.segment, text, n)
            record("barge_in_resumed")
            await self._end_barge_in("resume")
            return
        kind = interruption_kind(text, wake)
        text = from_name(text, wake)  # sem a sobra de eco antes do nome
        await self._stop_response(n, played_ms)
        await self._send(protocol.barge_in_end(n, "stop", event.segment))
        if kind == "stop":
            log.info("[%s] #%d barge-in: parar (%r)", self.id, event.segment, text)
            record("barge_in_stop")
            self.state = self.LISTENING
            self.segmenter.reset()
            self._awake_until = time.monotonic() + wake.conversation_s
        else:
            log.info("[%s] #%d barge-in: pergunta nova (%r)", self.id, event.segment, text)
            record("response")
            self._start_response(event, text, finished)

    async def _end_barge_in(self, action: str) -> None:
        """Não era com ela: a resposta continua de onde parou."""
        n, played_ms = self._barge["response"], self._barge["played_ms"]
        self._barge = None
        self._dropped |= self._confirm_segments  # as que ainda não decidiram: o resultado já foi dado
        if self._barge_timer:
            self._barge_timer.cancel()
            self._barge_timer = None
        self.state = self.RESPONDING
        self.segmenter.reset()
        await self._send(protocol.barge_in_end(n, action))
        if self._response_task and self._response_task.done():
            # o prazo do playback_end conta o que falta tocar a partir de agora
            remaining_s = max(0.0, self._audio_ms - played_ms) / 1000
            if self._resume_timer:
                self._resume_timer.cancel()
            self._resume_timer = asyncio.get_running_loop().call_later(
                remaining_s + self.server.playback_grace_s, self._resume_after_timeout, n)

    async def _stop_response(self, n: int, played_ms: float) -> None:
        """Cancela o que ainda estiver sendo gerado e deixa no histórico só o que foi ouvido."""
        self._barge = None
        if self._barge_timer:
            self._barge_timer.cancel()
            self._barge_timer = None
        if self._resume_timer:
            self._resume_timer.cancel()
            self._resume_timer = None
        task = self._response_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        spoken = " ".join(text for text, start_ms in self._chunks if start_ms < played_ms)
        self.conversation.interrupt(spoken)
        if self._pending_turn is not None:
            self._pending_turn.update(reason="interrupted", heard=spoken, played_ms=round(played_ms))
            self._log_response(None, "interrupted")
        else:
            self._log("response", response=n, reason="interrupted", text=spoken, heard=spoken,
                      played_ms=round(played_ms), playback="interrupted")
        self.current_response = None

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
        self._log_response(e2e_ms, "played")
        self._resume()

    def _resume_after_timeout(self, n: int) -> None:
        if self.state == self.RESPONDING and self.current_response == n:
            log.warning("[%s] playback_end %d não chegou; voltando a ouvir", self.id, n)
            self._log_response(None, "timeout")
            self._resume()

    def _log(self, event: str, **fields) -> None:
        if self.server.turn_log is not None:
            self.server.turn_log.write(event, session=self.id, **fields)

    def _log_response(self, e2e_ms: float | None, playback: str) -> None:
        """Registra a resposta pendente, com o tempo medido no cliente (se veio) e como terminou."""
        if self._pending_turn is not None:
            turn, self._pending_turn = self._pending_turn, None
            self._log("response", **turn, e2e_ms=round(e2e_ms) if e2e_ms is not None else None, playback=playback)

    def _resume(self) -> None:
        if self._resume_timer:
            self._resume_timer.cancel()
            self._resume_timer = None
        self.segmenter.reset()
        self.state = self.LISTENING
        self.current_response = None
        # modo conversa: por alguns segundos, não precisa chamar pelo nome
        self._awake_until = time.monotonic() + self.server.config.wake.conversation_s


def build_tools(memory: MemoryStore, zabbix: ZabbixClient | None = None,
                oracle: OracleClient | None = None, oracle_cdb: OracleClient | None = None,
                github: GitHubClient | None = None, weather: WeatherClient | None = None,
                news: NewsClient | None = None) -> ToolRegistry:
    tools = [calculator, *memory_tools(memory)]
    if zabbix is not None:
        tools += zabbix_tools(zabbix)
    if oracle is not None:
        tools += oracle_tools(oracle.query, oracle_cdb.query if oracle_cdb is not None else None)
    if github is not None:
        tools += github_tools(github)
    if weather is not None:
        tools += weather_tools(weather)
    if news is not None:
        tools += news_tools(news)
    return ToolRegistry(tools)


async def run(config: ServerConfig, transcriber: SupportsTranscribe, llm, synthesizer) -> None:
    tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
    memory = MemoryStore(config.memory)
    zabbix = ZabbixClient(config.zabbix) if config.zabbix.enabled else None
    oracle = OracleClient(config.oracle) if config.oracle.enabled else None
    oracle_cdb = OracleClient(config.oracle, cdb=True) if config.oracle.enabled and config.oracle.cdb_enabled else None
    github = GitHubClient(config.github) if config.github.enabled else None
    weather = WeatherClient(config.weather) if config.weather.enabled else None
    news = NewsClient(config.news) if config.news.enabled else None
    tools = build_tools(memory, zabbix, oracle, oracle_cdb, github, weather, news)
    log.info("ferramentas: %s · memória: %d fatos em %s", ", ".join(tools.tools), len(memory.facts()),
             config.memory.db)
    turn_log = TurnLog(config.log_dir) if config.log_dir else None
    if turn_log:
        log.info("registro de turnos em %s", config.log_dir)
    server = AgathaServer(
        config,
        transcriber,
        conversation_factory=lambda: Conversation(llm, synthesizer, tts_executor, config.conversation,
                                                  tools=tools, memory=memory),
        output_sample_rate=synthesizer.sample_rate,
        turn_log=turn_log,
    )
    if await llm.healthy():
        log.info("LLM disponível em %s", config.llm.url)
        # Deixa o começo fixo do prompt (sistema, exemplos e ferramentas) no cache do llama.cpp: sem
        # isso, a 1ª pergunta depois de subir o LLM paga ~1,5 s processando o prompt inteiro.
        t0 = time.monotonic()
        try:
            await llm.prefill(build_prefix(tools, memory), tools.schemas() or None)
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
        memory.close()
        for client in (zabbix, oracle, oracle_cdb, github, weather, news):
            if client is not None:
                await client.aclose()
        await llm.aclose()
