"""Definição de ferramenta e registro.

Cada ferramenta traz, além do esquema para o LLM, a regra que entra no prompt ("quando usar") e os
seus próprios exemplos de uso (few-shot). Os exemplos são decisivos para o Gemma 4 E4B chamar as
ferramentas de forma consistente (bench/tools_bench.py, ADR-009) e só podem mencionar ferramentas
que existem: por isso vivem junto delas.
"""

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TOOL_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Example:
    question: str
    arguments: dict
    result: dict
    answer: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[[dict], dict | Awaitable[dict]]
    rule: str  # linha da nota do prompt: quando usar esta ferramenta
    properties: dict = field(default_factory=dict)
    required: tuple[str, ...] = ()
    examples: tuple[Example, ...] = ()

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": self.properties, "required": list(self.required)},
        }}


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def __bool__(self) -> bool:
        return bool(self.tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    async def execute(self, name: str, arguments: str) -> dict:
        """Executa a ferramenta; qualquer problema vira {"erro": ...} para o LLM explicar."""
        tool = self.tools.get(name)
        if tool is None:
            return {"erro": f"ferramenta desconhecida: {name}"}
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("argumentos devem ser um objeto")
        except ValueError as e:
            return {"erro": f"argumentos inválidos: {e}"}
        try:
            result = tool.handler(args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, TOOL_TIMEOUT_S)
            return result
        except Exception as e:
            log.exception("ferramenta %s falhou", name)
            return {"erro": f"{type(e).__name__}: {e}"}
