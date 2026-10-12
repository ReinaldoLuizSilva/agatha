"""Cliente do llama-server (API compatível com OpenAI, em streaming). Ver ADR-004."""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    url: str = "http://127.0.0.1:8080"
    temperature: float = 0.6
    max_tokens: int = 200


# Colchetes proibidos na resposta. O modelo às vezes repete o carimbo "[segunda-feira, 12/10/2026, 10h59]"
# da pergunta antes de responder: ~25 tokens (~1,4 s) gerados e jogados fora antes do 1º som. A fala
# nunca usa colchetes. Em string, o llama-server tokeniza com o vocabulário do modelo carregado.
NO_BRACKETS = [[s, False] for s in ("[", " [", "[[", " [[")]


class ContextOverflow(Exception):
    """O prompt não cabe no contexto do llama-server (exceed_context_size_error)."""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """Pedaço de uma chamada de ferramenta; o 1º traz `name`, os seguintes trazem `arguments`."""

    index: int
    id: str | None
    name: str | None
    arguments: str


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(base_url=config.url, timeout=httpx.Timeout(60.0, connect=5.0))

    def _body(self, messages: list[dict], tools: list[dict] | None, **extra) -> dict:
        body = {
            "messages": messages,
            "cache_prompt": True,  # reaproveita o prefixo do prompt já processado
            "chat_template_kwargs": {"enable_thinking": False},
            **extra,
        }
        if tools:
            # as ferramentas entram no prompt pelo template: precisam ser sempre as mesmas para o cache valer
            body["tools"] = tools
            body["parallel_tool_calls"] = False
        return body

    async def stream_chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[TextDelta | ToolCallDelta]:
        """Gera os pedaços da resposta (texto ou chamada de ferramenta) à medida que o modelo produz."""
        body = self._body(messages, tools, stream=True, max_tokens=self.config.max_tokens,
                          temperature=self.config.temperature, logit_bias=NO_BRACKETS)
        async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
            if response.is_error:
                await response.aread()
                if "exceed_context_size" in response.text:
                    raise ContextOverflow(response.text[:200])
                raise httpx.HTTPStatusError(
                    f"llama-server respondeu {response.status_code}: {response.text[:200]}",
                    request=response.request, response=response,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or []
                delta = choices[0].get("delta", {}) if choices else {}
                if delta.get("content"):
                    yield TextDelta(delta["content"])
                for call in delta.get("tool_calls") or []:
                    function = call.get("function") or {}
                    yield ToolCallDelta(call.get("index", 0), call.get("id"), function.get("name"),
                                        function.get("arguments") or "")

    async def prefill(self, messages: list[dict], tools: list[dict] | None = None) -> None:
        """Processa um prompt só para deixá-lo no cache do llama.cpp (gera 1 token e descarta)."""
        response = await self._client.post("/v1/chat/completions", json=self._body(messages, tools, max_tokens=1))
        response.raise_for_status()

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/health", timeout=3.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
