"""Ferramentas da memória explícita (ADR-010): guardar_fato e esquecer.

O nome não é "lembrar" porque, em português, "lembrar" também é "recordar": perguntas como "qual
padrão eu uso, lembra?" faziam o modelo chamar a ferramenta para regravar um fato já guardado.
Os exemplos usam números que não colidem com os primeiros fatos reais da memória.
"""

from ..memory import MemoryStore
from .base import Example, Tool

_STATUS_ERRORS = {
    "vazio": "fato vazio",
    "longo": "fato longo demais; resuma numa frase curta",
    "cheio": "a memória está cheia; peça para ele escolher algo para esquecer",
}


def memory_tools(store: MemoryStore) -> list[Tool]:
    def guardar_fato(args: dict) -> dict:
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
            name="guardar_fato",
            description="Guarda um fato NOVO sobre o Reinaldo na memória, para conversas futuras. Use quando "
                        "ele pedir para lembrar, anotar ou guardar alguma coisa que ainda não está na memória.",
            handler=guardar_fato,
            rule="quando ele pedir para lembrar, anotar ou guardar algo novo: use guardar_fato",
            properties={"fato": {"type": "string", "description": "o fato, numa frase curta, na 3ª pessoa"}},
            required=("fato",),
            examples=(Example("Lembra que meu notebook se chama rei-note.",
                              {"fato": "O notebook do Reinaldo se chama rei-note"}, {"ok": True, "numero": 41},
                              "Pode deixar, guardei."),),
        ),
        Tool(
            name="esquecer",
            description="Apaga um fato da memória, pelo número com que ele aparece na lista de memória.",
            handler=esquecer,
            rule="quando ele pedir para esquecer ou apagar algo da memória: use esquecer com o número do fato",
            properties={"numero": {"type": "integer", "description": "número do fato na memória"}},
            required=("numero",),
            examples=(Example("Esquece aquilo do notebook.", {"numero": 41},
                              {"ok": True, "esquecido": "O notebook do Reinaldo se chama rei-note"},
                              "Pronto, esqueci."),),
        ),
    ]
