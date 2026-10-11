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


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(base_url=config.url, timeout=httpx.Timeout(60.0, connect=5.0))

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """Gera os pedaços de texto da resposta à medida que o modelo produz."""
        body = {
            "messages": messages,
            "stream": True,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "cache_prompt": True,  # reaproveita o prompt de sistema já processado
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or []
                delta = choices[0].get("delta", {}).get("content") if choices else None
                if delta:
                    yield delta

    async def prefill(self, messages: list[dict]) -> None:
        """Processa um prompt só para deixá-lo no cache do llama.cpp (gera 1 token e descarta)."""
        body = {
            "messages": messages,
            "max_tokens": 1,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = await self._client.post("/v1/chat/completions", json=body)
        response.raise_for_status()

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/health", timeout=3.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
