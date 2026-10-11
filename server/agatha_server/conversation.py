"""Orquestra uma resposta: LLM em streaming → trechos → limpeza → TTS, com histórico curto.

Cada sessão (conexão) tem a sua `Conversation`. O histórico vive só na memória e é perdido
quando a conexão fecha; memória de longo prazo fica para a Fase 3.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .prompts import system_message
from .text import Chunker, clean, normalize_for_speech

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationConfig:
    history_turns: int = 6  # pares pergunta/resposta mantidos no contexto


class SupportsStreamChat(Protocol):
    def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]: ...


class SupportsSynthesize(Protocol):
    sample_rate: int

    def synthesize(self, text: str) -> bytes: ...


@dataclass(frozen=True)
class TextChunk:
    text: str  # limpo, para exibir


@dataclass(frozen=True)
class Audio:
    pcm: bytes  # s16le mono na taxa do sintetizador


@dataclass
class Done:
    reason: str  # "done" | "error"
    text: str
    timings: dict = field(default_factory=dict)  # ms a partir da referência (fim da fala)


class Conversation:
    def __init__(
        self,
        llm: SupportsStreamChat,
        synthesizer: SupportsSynthesize,
        tts_executor: Executor,
        config: ConversationConfig = ConversationConfig(),
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ):
        self._llm = llm
        self._synth = synthesizer
        self._tts_executor = tts_executor
        self.config = config
        self._clock = clock
        self._now = now
        self.history: list[dict] = []

    async def respond(self, user_text: str, t_ref: float) -> AsyncIterator[TextChunk | Audio | Done]:
        """Gera a resposta trecho a trecho. `t_ref` é o instante do fim da fala (relógio monotônico)."""
        messages = [system_message(self._now()), *self.history, {"role": "user", "content": user_text}]
        chunker = Chunker()
        timings: dict = {}
        spoken: list[str] = []
        loop = asyncio.get_running_loop()

        def ms_since_ref() -> int:
            return round((self._clock() - t_ref) * 1000)

        async def speak(raw_chunk: str) -> AsyncIterator[TextChunk | Audio]:
            text = clean(raw_chunk)
            if not text:
                return
            spoken.append(text)
            if "first_chunk_ms" not in timings:
                timings["first_chunk_ms"] = ms_since_ref()
            yield TextChunk(text)
            t0 = self._clock()
            pcm = await loop.run_in_executor(self._tts_executor, self._synth.synthesize, normalize_for_speech(text))
            if "tts_first_ms" not in timings:
                timings["tts_first_ms"] = round((self._clock() - t0) * 1000)
            if pcm:
                yield Audio(pcm)

        try:
            async for delta in self._llm.stream_chat(messages):
                if "llm_first_token_ms" not in timings:
                    timings["llm_first_token_ms"] = ms_since_ref()
                for raw_chunk in chunker.feed(delta):
                    async for event in speak(raw_chunk):
                        yield event
            for raw_chunk in chunker.flush():
                async for event in speak(raw_chunk):
                    yield event
        except Exception:
            log.exception("falha ao gerar a resposta")
            yield Done("error", " ".join(spoken), timings)
            return

        answer = " ".join(spoken)
        self._remember(user_text, answer)
        yield Done("done", answer, timings)

    def _remember(self, user_text: str, answer: str) -> None:
        self.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": answer}]
        self.history = self.history[-2 * self.config.history_turns:]
