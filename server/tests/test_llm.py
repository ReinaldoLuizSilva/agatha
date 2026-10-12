"""Cliente do llama-server com a API simulada (httpx.MockTransport)."""

import asyncio
import json

import httpx

from agatha_server.llm import NO_BRACKETS, LLMClient, LLMConfig, TextDelta, ToolCallDelta


def sse(*deltas: dict) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': d}]})}" for d in deltas]
    return ("\n\n".join([*lines, "data: [DONE]"]) + "\n\n").encode()


def test_stream_chat_proibe_colchetes_e_le_os_deltas():
    sent = {}

    def fake(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=sse(
            {"content": "São "}, {"content": "10h59."},
            {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calcular", "arguments": "{}"}}]},
        ))

    async def collect():
        llm = LLMClient(LLMConfig(url="http://llm"))
        llm._client = httpx.AsyncClient(base_url="http://llm", transport=httpx.MockTransport(fake))
        try:
            return [d async for d in llm.stream_chat([{"role": "user", "content": "oi"}])]
        finally:
            await llm.aclose()

    deltas = asyncio.run(collect())
    assert deltas == [TextDelta("São "), TextDelta("10h59."), ToolCallDelta(0, "c1", "calcular", "{}")]
    # o modelo às vezes repetia o carimbo "[segunda-feira, ...]" antes de responder (teste de 12/10)
    assert sent["logit_bias"] == NO_BRACKETS
    assert ["[", False] in sent["logit_bias"]
