"""Prompt de sistema da Agatha e contexto de data e hora.

Evolução (a v2 foi medida no ADR-004):
- v3: algarismos liberados (o núcleo normaliza para a fala; por extenso o modelo errava e
  alongava a 1ª frase; depois passou a exigir algarismos, porque o modelo errava a hora ao
  escrevê-la por extenso), pedido para repetir quando a pergunta parecer mal ouvida, e data/hora
  na mensagem do usuário em vez do prompt de sistema, para o prefixo do prompt nunca mudar e o
  llama.cpp reaproveitar o cache (o Gemma 4 usa sliding window attention: se o começo muda, o
  prompt inteiro é reprocessado).
- ferramentas (ADR-009): nota com regras explícitas de quando usar cada uma + exemplos de conversa
  no começo (few-shot), montados a partir das ferramentas registradas. Sem os exemplos, o Gemma 4
  E4B chamava as ferramentas em ~50% dos casos; com eles, 42/42 no bench/tools_bench.py.
"""

import json
from datetime import datetime

SYSTEM_PROMPT = """\
Você é a Agatha, assistente de voz pessoal. O usuário é o Reinaldo; você o chama de Reinaldo ou de Rei.
Tudo o que você escreve será falado em voz alta, então:
- comece sempre com uma frase bem curta, de no máximo seis palavras; depois complemente se for útil;
- responda em português do Brasil, em até três frases no total, a menos que ele peça detalhes;
- nunca leia código, SQL ou comandos completos: diga o nome do comando ou da view e explique a ideia;
- não use markdown, listas, emojis nem parênteses;
- escreva horas, datas e números sempre em algarismos, como 18h22, 11/10 ou 36; o sistema converte para a fala;
- não cumprimente nem repita o nome dele em toda resposta, só quando soar natural;
- tom meio a meio entre profissional e descontraído: cordial e direto;
- se não souber algo ou não tiver acesso (internet, sistemas), diga isso em vez de inventar.
O que ele diz chega por reconhecimento de voz e pode vir com palavras trocadas. Se a pergunta
parecer sem sentido ou citar algo que você não reconhece, diga que não entendeu e peça para repetir.
Cada mensagem dele começa com a data e a hora atuais entre colchetes; use só se for relevante.
O Reinaldo trabalha com infraestrutura (Linux, Oracle, Zabbix) e estuda cloud e DevOps."""

_WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def describe_now(now: datetime) -> str:
    """Data e hora no formato brasileiro curto (poucos tokens), sem depender do locale."""
    return f"{_WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y}, {now.hour}h{now.minute:02d}"


_CONCEPTS_RULE = (
    'Perguntas do tipo "o que é", "como funciona" ou "para que serve" são sobre conceitos: responda direto '
    "com o que você sabe, sem ferramenta. O mesmo vale para opiniões e conversa."
)

# Exemplos de perguntas conceituais respondidas sem ferramenta (evitam que o modelo ache que tudo
# sobre Zabbix ou Oracle precisa de ferramenta: sem eles, "o que é um trigger?" virava "não sei").
_CONCEPT_EXAMPLES = [
    ("O que é um índice num banco de dados?", "É uma estrutura que acelera as buscas. Funciona como o índice de um livro."),
    ("Para que serve um template no Zabbix?",
     "Ele agrupa itens, triggers e gráficos prontos. Você aplica em vários hosts de uma vez, sem configurar um por um."),
]
_EXAMPLE_NOW = datetime(2026, 10, 10, 9, 0)


_EXAMPLES_NOTE = ("As primeiras trocas desta conversa são só exemplos de formato: não são fatos sobre o "
                  "Reinaldo nem coisas que ele disse.")


def memory_block(facts) -> str:
    """Fatos da memória (ADR-010), numerados como a ferramenta `esquecer` espera."""
    if not facts:
        return "Memória: nada guardado ainda."
    lines = "\n".join(f"{f.id}. {f.text}" for f in facts)
    return f"Memória (fatos que o Reinaldo pediu para você lembrar; use quando forem relevantes):\n{lines}"


def system_message(tools=None, facts=None) -> dict:
    """Prompt de sistema; com ferramentas, inclui as regras de quando usar cada uma; com memória
    (`facts` não None), inclui os fatos guardados."""
    content = SYSTEM_PROMPT
    if tools:
        rules = "\n".join(f"- {t.rule};" for t in tools.tools.values())
        content += f"\nVocê tem ferramentas e deve usá-las nestes casos, sempre:\n{rules}\n{_CONCEPTS_RULE}"
        content += f"\n{_EXAMPLES_NOTE}"
    if facts is not None:
        content += f"\n{memory_block(facts)}"
    return {"role": "system", "content": content}


def few_shot_messages(tools) -> list[dict]:
    """Conversa de exemplo que vai fixa no começo do prompt (entra no cache do prefixo)."""
    if not tools:
        return []
    messages: list[dict] = []
    for tool in tools.tools.values():
        for i, ex in enumerate(tool.examples):
            call_id = f"exemplo_{tool.name}_{i}"
            messages += [
                user_message(ex.question, _EXAMPLE_NOW),
                {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": tool.name, "arguments": json.dumps(ex.arguments, ensure_ascii=False)}}]},
                {"role": "tool", "tool_call_id": call_id, "content": json.dumps(ex.result, ensure_ascii=False)},
                {"role": "assistant", "content": ex.answer},
            ]
    for question, answer in _CONCEPT_EXAMPLES:
        messages += [user_message(question, _EXAMPLE_NOW), {"role": "assistant", "content": answer}]
    return messages


def user_message(text: str, now: datetime) -> dict:
    return {"role": "user", "content": f"[{describe_now(now)}] {text}"}
