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

from .prompts import system_message, user_message
from .text import Chunker, clean, normalize_for_speech

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationConfig:
    # Pares pergunta/resposta mantidos no contexto. O corte é em bloco: ao passar de
    # `history_turns`, volta para a metade. Cortar uma troca por turno mudaria o começo do prompt
    # a cada turno, e o llama.cpp reprocessaria tudo (Gemma 4 usa sliding window attention):
    # medido no teste do Windows, 525 tokens em 2,1 s na 8ª pergunta.
    history_turns: int = 12


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
        user = user_message(user_text, self._now())
        messages = [system_message(), *self.history, user]
        chunker = Chunker()
        timings: dict = {}
        spoken: list[str] = []
        raw: list[str] = []  # texto exato do modelo, para o histórico bater com o cache do llama.cpp
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
                raw.append(delta)
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

        self._remember(user, "".join(raw))
        yield Done("done", " ".join(spoken), timings)

    def _remember(self, user: dict, raw_answer: str) -> None:
        # Guarda exatamente o que foi enviado e gerado: qualquer diferença no prefixo do prompt
        # obriga o llama.cpp a reprocessar (ver prompts.py).
        self.history += [user, {"role": "assistant", "content": raw_answer}]
        if len(self.history) > 2 * self.config.history_turns:
            keep = max(1, self.config.history_turns // 2)
            self.history = self.history[-2 * keep:]
