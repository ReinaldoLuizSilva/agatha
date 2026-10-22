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
- v4 (Fase 6, ADR-018, bench/precision_bench.py): o "se não souber, diga" e o "se não reconhecer,
  peça para repetir" eram generalizados pelo modelo ("não tenho informações" sobre CDB e PDB, "não
  tenho acesso a sistemas" tendo o Zabbix). Agora: conceito se responde; "não sei" só para o que
  nenhuma ferramenta mostra; termo estranho → o termo técnico de som parecido, confirmado; glossário
  das siglas dele; acesso às ferramentas explícito.
- v8 (20/10, ADR-018 "Revisão de 20/10"): no meio de uma conversa a v5 inventava sigla mal ouvida
  ("OWR" → "Oracle WebLogic Repository"); sigla de Oracle/Zabbix fora do glossário → confirma a dele.
- v12 (21/10, ADR-018): recusava conhecimento geral ("Mona Lisa", "fotossíntese", "El Niño" → "não sei, sou
  assistente de TI"): "para o trabalho e o dia a dia", "conhecimento geral ou conceitos de TI", termo de som
  parecido "de TI ou não" e um exemplo de conhecimento geral (Dom Casmurro).
"""

import json
from datetime import datetime

SYSTEM_PROMPT = """\
Você é a Agatha, assistente de voz pessoal para o trabalho e o dia a dia. O usuário é o Reinaldo; você o chama de Reinaldo ou de Rei.
Tudo o que você escreve será falado em voz alta, então:
- comece sempre com uma frase bem curta, de no máximo seis palavras; depois complemente se for útil;
- responda em português do Brasil, em até três frases no total, a menos que ele peça detalhes;
- nunca leia código, SQL ou comandos completos: diga o nome do comando ou da view e explique a ideia;
- não use markdown, listas, emojis nem parênteses;
- escreva horas, datas e números sempre em algarismos, como 18h22, 11/10 ou 36; o sistema converte para a fala;
- não cumprimente nem repita o nome dele em toda resposta, só quando soar natural;
- tom meio a meio entre profissional e descontraído: cordial e direto;
- perguntas de conhecimento geral ou sobre conceitos de TI você responde direto com o que sabe, sem avisar que não tem detalhes; diga que
  não sabe só quando for algo do ambiente dele que nenhuma ferramenta mostra, como o estado de um backup ou de
  locks, e nunca invente um estado.
O que ele diz chega por reconhecimento de voz e pode vir com palavras trocadas, principalmente siglas e nomes
técnicos. Se uma palavra não fizer sentido, pense no termo de som parecido, de TI ou não, e confirme numa frase curta,
como "Você quis dizer Data Guard?". Se não achar nenhum, diga que não entendeu e peça para repetir. Nunca
explique uma palavra que você não reconhece como termo técnico.
Cada mensagem dele começa com a data e a hora atuais entre colchetes; use só se for relevante.
O Reinaldo trabalha com infraestrutura (Linux, Oracle, Zabbix) e estuda cloud e DevOps. Siglas e termos que ele
usa: no Oracle, RMAN, AWR, ASH, ASM, CDB, PDB, Data Guard, SPFILE, archivelog, listener; no Zabbix, item,
trigger, template, housekeeping, proxy; tipos de backup: completo, incremental e diferencial. Uma sigla de
Oracle ou Zabbix fora dessa lista quase sempre é uma delas mal ouvida: confirme ("Você quis dizer AWR?" para
"OWR"), também no meio de uma conversa."""

_WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def describe_now(now: datetime) -> str:
    """Data e hora no formato brasileiro curto (poucos tokens), sem depender do locale."""
    return f"{_WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y}, {now.hour}h{now.minute:02d}"


_CONCEPTS_RULE = (
    'Perguntas do tipo "o que é", "como funciona" ou "para que serve" são sobre conceitos: responda direto '
    "com o que você sabe, sem ferramenta. O mesmo vale para opiniões, conversa e perguntas sobre o que já está "
    "na sua memória."
)

# Exemplos de perguntas conceituais respondidas sem ferramenta (evitam que o modelo ache que tudo
# sobre Zabbix ou Oracle precisa de ferramenta: sem eles, "o que é um trigger?" virava "não sei").
_CONCEPT_EXAMPLES = [
    ("O que é um índice num banco de dados?", "É uma estrutura que acelera as buscas. Funciona como o índice de um livro."),
    ("Para que serve um template no Zabbix?",
     "Ele agrupa itens, triggers e gráficos prontos. Você aplica em vários hosts de uma vez, sem configurar um por um."),
    # conhecimento geral também se responde (21/10: "Mona Lisa", "fotossíntese" e "El Niño" viravam "sou assistente de TI")
    ("Quem escreveu Dom Casmurro?", "Foi o Machado de Assis, em 1899."),
]
_EXAMPLE_NOW = datetime(2026, 10, 10, 9, 0)


_TOOLS_NOTE = ("Pelas ferramentas você acessa esses sistemas de verdade: nunca diga que não tem acesso a eles. Só "
               "chame uma ferramenta se ela mostra o que ele perguntou. Se o resultado não trouxer o que ele "
               "perguntou, diga que ela não mostra isso, sem tirar conclusões.")

_EXAMPLES_NOTE = ("As primeiras trocas desta conversa são só exemplos de formato: não são fatos sobre o "
                  "Reinaldo nem coisas que ele disse.")


def memory_block(facts) -> str:
    """Fatos da memória (ADR-010), numerados como a ferramenta `esquecer` espera."""
    if not facts:
        return "Sua memória sobre o Reinaldo está vazia: ele ainda não pediu para você guardar nada."
    lines = "\n".join(f"{f.id}. {f.text}" for f in facts)
    return (
        "Sua memória: fatos que o Reinaldo pediu para você guardar. Use-os ao responder. Se ele perguntar o que "
        "você sabe sobre ele, conte estes fatos. Para apagar um deles, use esquecer com o número. Não use "
        f"guardar_fato para algo que já está aqui.\n{lines}"
    )


def system_message(tools=None, facts=None) -> dict:
    """Prompt de sistema; com ferramentas, inclui as regras de quando usar cada uma; com memória
    (`facts` não None), inclui os fatos guardados."""
    content = SYSTEM_PROMPT
    if facts is not None:
        # logo depois da apresentação: no fim de um prompt longo o modelo ignorava os fatos
        content += f"\n{memory_block(facts)}"
    if tools:
        rules = "\n".join(f"- {t.rule};" for t in tools.tools.values())
        content += f"\nVocê tem ferramentas e deve usá-las nestes casos, sempre:\n{rules}\n{_TOOLS_NOTE}\n{_CONCEPTS_RULE}"
        content += f"\n{_EXAMPLES_NOTE}"
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
