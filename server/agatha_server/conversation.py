"""Orquestra uma resposta: LLM em streaming → trechos → limpeza → TTS, com histórico curto.

Com ferramentas (ADR-009): se o LLM pede uma ferramenta, a Agatha fala uma frase curta de espera
("Deixa eu ver."), executa a ferramenta e volta ao LLM com o resultado, até `max_tool_rounds`.

Cada sessão (conexão) tem a sua `Conversation`. O histórico vive só na memória e é perdido
quando a conexão fecha.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime
from itertools import cycle
from typing import Protocol

from .llm import ContextOverflow, TextDelta, ToolCallDelta
from .memory import MemoryStore
from .prompts import few_shot_messages, system_message, user_message
from .text import Chunker, LeadingTimestampFilter, clean, normalize_for_speech
from .tools import ToolRegistry

log = logging.getLogger(__name__)

# Frases ditas enquanto uma ferramenta roda (alternadas para não soar repetitivo)
FILLERS = ("Deixa eu ver.", "Um instante.", "Só um momento.")


@dataclass(frozen=True)
class ConversationConfig:
    # Trocas (turnos) mantidas no contexto. O corte é em bloco: ao passar de `history_turns`,
    # volta para a metade. Cortar uma troca por turno mudaria o começo do prompt a cada turno, e o
    # llama.cpp reprocessaria tudo (Gemma 4 usa sliding window attention): medido no teste do
    # Windows, 525 tokens em 2,1 s na 8ª pergunta.
    history_turns: int = 12
    max_tool_rounds: int = 2  # idas ao LLM com resultado de ferramenta, por resposta


class SupportsStreamChat(Protocol):
    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None
                    ) -> AsyncIterator[TextDelta | ToolCallDelta]: ...

    async def prefill(self, messages: list[dict], tools: list[dict] | None = None) -> None: ...


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
    tools: list[str] = field(default_factory=list)  # ferramentas usadas, na ordem


def build_prefix(tools: ToolRegistry | None, memory: MemoryStore | None = None) -> list[dict]:
    """Começo do prompt: sistema (+ regras das ferramentas + memória) e exemplos. Entra no cache do
    LLM; só muda quando a memória muda."""
    facts = memory.facts() if memory is not None else None
    return [system_message(tools, facts), *few_shot_messages(tools)]


class Conversation:
    def __init__(
        self,
        llm: SupportsStreamChat,
        synthesizer: SupportsSynthesize,
        tts_executor: Executor,
        config: ConversationConfig = ConversationConfig(),
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        tools: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
    ):
        self._llm = llm
        self._synth = synthesizer
        self._tts_executor = tts_executor
        self.config = config
        self._clock = clock
        self._now = now
        self._tools = tools or ToolRegistry([])
        self._schemas = self._tools.schemas() or None
        self._memory = memory
        self._prefix = build_prefix(self._tools, memory)
        self._prefix_version = memory.version if memory else 0
        self._fillers = cycle(FILLERS)
        self.history: list[list[dict]] = []  # um item por turno: as mensagens exatas daquele turno
        self._prefill_task: asyncio.Task | None = None

    def _flat_history(self) -> list[dict]:
        return [m for turn in self.history for m in turn]

    def _refresh_prefix(self) -> bool:
        """Refaz o começo do prompt se a memória mudou. Devolve True se mudou."""
        if self._memory is None or self._memory.version == self._prefix_version:
            return False
        self._prefix = build_prefix(self._tools, self._memory)
        self._prefix_version = self._memory.version
        return True

    async def respond(self, user_text: str, t_ref: float) -> AsyncIterator[TextChunk | Audio | Done]:
        """Gera a resposta trecho a trecho. `t_ref` é o instante do fim da fala (relógio monotônico)."""
        # O começo do prompt fica congelado durante o turno: se a memória mudar agora (lembrar,
        # esquecer), a mudança vale a partir do próximo turno, sem reprocessar o prompt no meio.
        self._refresh_prefix()
        turn: list[dict] = [user_message(user_text, self._now())]
        timings: dict = {}
        spoken: list[str] = []
        used_tools: list[str] = []
        loop = asyncio.get_running_loop()
        tools_allowed = self._schemas is not None

        def ms_since_ref() -> int:
            return round((self._clock() - t_ref) * 1000)

        async def speak(raw_chunk: str) -> AsyncIterator[TextChunk | Audio]:
            text = clean(raw_chunk)
            if not text:
                return
            spoken.append(text)
            timings.setdefault("first_chunk_ms", ms_since_ref())
            yield TextChunk(text)
            t0 = self._clock()
            pcm = await loop.run_in_executor(self._tts_executor, self._synth.synthesize, normalize_for_speech(text))
            timings.setdefault("tts_first_ms", round((self._clock() - t0) * 1000))
            if pcm:
                yield Audio(pcm)

        rounds = 0
        while True:
            messages = [*self._prefix, *self._flat_history(), *turn]
            tools = self._schemas if tools_allowed and rounds < self.config.max_tool_rounds else None
            chunker = Chunker()
            head = LeadingTimestampFilter()  # o modelo às vezes repete o carimbo de data da pergunta
            raw: list[str] = []  # texto exato do modelo, para o histórico bater com o cache do llama.cpp
            calls: dict[int, dict] = {}
            try:
                async for delta in self._llm.stream_chat(messages, tools):
                    timings.setdefault("llm_first_token_ms", ms_since_ref())
                    if isinstance(delta, TextDelta):
                        raw.append(delta.text)
                        for raw_chunk in chunker.feed(head.feed(delta.text)):
                            async for event in speak(raw_chunk):
                                yield event
                        continue
                    first_piece = not calls
                    call = calls.setdefault(delta.index, {"id": None, "name": "", "arguments": ""})
                    call["id"] = call["id"] or delta.id
                    call["name"] += delta.name or ""
                    call["arguments"] += delta.arguments
                    if first_piece and not spoken:  # vai consultar algo: não deixa o silêncio no ar
                        async for event in speak(next(self._fillers)):
                            yield event
                for raw_chunk in [*chunker.feed(head.flush()), *chunker.flush()]:
                    async for event in speak(raw_chunk):
                        yield event
            except ContextOverflow:
                if self.history and not spoken:
                    # O prompt não coube (prefixo grande + histórico longo): fica com os 2 últimos turnos
                    log.warning("prompt maior que o contexto do LLM; cortando o histórico de %d para 2 turnos",
                                len(self.history))
                    self.history = self.history[-2:]
                    continue
                log.exception("prompt maior que o contexto do LLM, mesmo sem histórico")
                yield Done("error", " ".join(spoken), timings, used_tools)
                return
            except Exception:
                if tools is not None and not calls and not raw:
                    # ex.: argumento com JSON degenerado → HTTP 500 no llama-server; tenta sem ferramentas
                    log.exception("falha do LLM com ferramentas; tentando sem")
                    tools_allowed = False
                    continue
                log.exception("falha ao gerar a resposta")
                yield Done("error", " ".join(spoken), timings, used_tools)
                return

            if not calls:
                turn.append({"role": "assistant", "content": "".join(raw)})
                break

            tool_calls = [
                {"id": c["id"] or f"call_{rounds}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for i, c in sorted(calls.items())
            ]
            turn.append({"role": "assistant", "content": "".join(raw), "tool_calls": tool_calls})
            t0 = self._clock()
            for tc in tool_calls:
                name, args = tc["function"]["name"], tc["function"]["arguments"]
                result = await self._tools.execute(name, args)
                used_tools.append(name)
                log.info("ferramenta %s(%s) → %s", name, args, json.dumps(result, ensure_ascii=False)[:300])
                turn.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
            timings["tool_ms"] = timings.get("tool_ms", 0) + round((self._clock() - t0) * 1000)
            rounds += 1

        self._remember(turn)
        yield Done("done", " ".join(spoken), timings, used_tools)

    def _remember(self, turn: list[dict]) -> None:
        # Guarda exatamente o que foi enviado e gerado (inclusive chamadas e resultados de
        # ferramentas): qualquer diferença no prefixo do prompt obriga o llama.cpp a reprocessar.
        self.history.append(turn)
        reasons = []
        if len(self.history) > self.config.history_turns:
            keep = max(1, self.config.history_turns // 2)
            self.history = self.history[-keep:]
            reasons.append("corte do histórico")
        if self._refresh_prefix():
            reasons.append("memória alterada")
        if reasons:
            # O cache do llama.cpp foi invalidado. Enquanto a resposta ainda está sendo falada
            # (vários segundos), recalcula o novo prompt em segundo plano, para a próxima pergunta
            # não pagar ~2-3 s de reprocessamento.
            self._prefill_task = asyncio.create_task(
                self._prefill([*self._prefix, *self._flat_history()], " e ".join(reasons))
            )

    async def _prefill(self, messages: list[dict], reason: str) -> None:
        t0 = self._clock()
        try:
            await self._llm.prefill(messages, self._schemas)
            log.info("cache do LLM recalculado após %s (%.0f ms)", reason, (self._clock() - t0) * 1000)
        except Exception as e:
            log.warning("falha ao recalcular o cache do LLM: %s", e)
