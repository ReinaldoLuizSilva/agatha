"""Ferramentas da memória explícita (ADR-010): lembrar e esquecer."""

from ..memory import MemoryStore
from .base import Example, Tool

_STATUS_ERRORS = {
    "vazio": "fato vazio",
    "longo": "fato longo demais; resuma numa frase curta",
    "cheio": "a memória está cheia; peça para ele escolher algo para esquecer",
}


def memory_tools(store: MemoryStore) -> list[Tool]:
    def lembrar(args: dict) -> dict:
        fact, status = store.add(str(args.get("fato", "")))
        if status == "novo":
            return {"ok": True, "numero": fact.id}
        if status == "repetido":
            return {"ok": True, "ja_sabia": True, "numero": fact.id}
        return {"erro": _STATUS_ERRORS[status]}

    def esquecer(args: dict) -> dict:
        try:
            fact_id = int(args.get("numero"))
        except (TypeError, ValueError):
            return {"erro": "informe o número do fato, como aparece na memória"}
        fact = store.forget(fact_id)
        return {"ok": True, "esquecido": fact.text} if fact else {"erro": f"não existe fato número {fact_id}"}

    return [
        Tool(
            name="lembrar",
            description="Guarda um fato sobre o Reinaldo para lembrar em conversas futuras. Use quando ele "
                        "pedir para lembrar, anotar ou guardar alguma coisa.",
            handler=lembrar,
            rule="quando ele pedir para lembrar, anotar ou guardar algo: use lembrar",
            properties={"fato": {"type": "string", "description": "o fato, numa frase curta, na 3ª pessoa"}},
            required=("fato",),
            examples=(Example("Lembra que meu notebook se chama rei-note.",
                              {"fato": "O notebook do Reinaldo se chama rei-note"}, {"ok": True, "numero": 1},
                              "Pode deixar, guardei."),),
        ),
        Tool(
            name="esquecer",
            description="Apaga um fato da memória, pelo número com que ele aparece na lista de memória.",
            handler=esquecer,
            rule="quando ele pedir para esquecer ou apagar algo da memória: use esquecer com o número do fato",
            properties={"numero": {"type": "integer", "description": "número do fato na memória"}},
            required=("numero",),
            examples=(Example("Esquece aquilo do notebook.", {"numero": 1},
                              {"ok": True, "esquecido": "O notebook do Reinaldo se chama rei-note"},
                              "Pronto, esqueci."),),
        ),
    ]
