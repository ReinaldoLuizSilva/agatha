"""Benchmark de chamada de ferramentas (tool calling) do LLM da Agatha.

Usa o llama-server no ar (pod `agatha`, porta 8080 do host) com o prompt de sistema do servidor e
um conjunto de ferramentas, e mede para cada pergunta:
- se o modelo decidiu certo: chamar a ferramenta esperada, ou responder direto quando não precisa;
- se os argumentos fazem sentido (verificação por pergunta);
- tempo até a decisão (1º token de texto ou 1º delta de tool_call): é quando a Agatha pode
  começar a falar, ou dizer "Deixa eu ver." enquanto consulta;
- nas perguntas com ferramenta: a 2ª ida ao LLM com o resultado (simulado), tempo até o 1º trecho
  e se a resposta usou o resultado.

Uso:
    .venv/bin/python bench/tools_bench.py
    .venv/bin/python bench/tools_bench.py --runs 3      # repete para ver a consistência
"""

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from agatha_server.prompts import system_message, user_message

URL = "http://127.0.0.1:8080/v1/chat/completions"
RESULTS_DIR = Path(__file__).parent / "results"
NOW = datetime(2026, 10, 11, 21, 30)

TOOLS_NOTES = {
    "v1": """
Você tem ferramentas. Use uma quando precisar de dados reais (Zabbix, Oracle), de uma conta, ou
quando ele pedir para lembrar algo. Para explicar conceitos ou conversar, responda direto, sem ferramenta.""",
    "v2": """
Você tem ferramentas e deve usá-las nestes casos, sempre:
- qualquer conta, porcentagem ou conversão de unidades: use calcular, mesmo que pareça fácil;
- qualquer pergunta sobre o estado atual do Zabbix ou do Oracle (alertas, hosts, tablespaces, sessões):
  use a ferramenta correspondente; nunca invente nem diga que não tem acesso;
- quando ele pedir para lembrar, anotar ou guardar algo: use lembrar.
Para explicar conceitos, dar opiniões ou conversar, responda direto, sem ferramenta.""",
    "v3": """
Você tem ferramentas e deve usá-las nestes casos, sempre:
- qualquer conta, porcentagem ou conversão de unidades: use calcular, mesmo que pareça fácil;
- perguntas sobre o estado atual do Zabbix ou do Oracle (alertas agora, um host, uso das tablespaces,
  sessões abertas): use a ferramenta correspondente; nunca invente esses dados;
- quando ele pedir para lembrar, anotar ou guardar algo: use lembrar.
Perguntas do tipo "o que é", "como funciona" ou "para que serve" são sobre conceitos: responda direto com
o que você sabe, sem ferramenta. O mesmo vale para opiniões e conversa.""",
}


def fn(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }}


TOOLS = [
    fn("calcular", "Calcula uma expressão matemática. Use para qualquer conta, porcentagem ou conversão "
       "numérica. Exemplo de expressão: 240 * 0.15",
       {"expressao": {"type": "string", "description": "expressão em sintaxe Python"}}, ["expressao"]),
    fn("zabbix_problemas", "Lista os alertas (problemas) ativos no Zabbix, de todos os hosts ou de um host.",
       {"host": {"type": "string", "description": "nome do host; omita para todos"}}),
    fn("zabbix_host", "Mostra o estado e as métricas atuais de um host monitorado no Zabbix: CPU, memória, disco.",
       {"host": {"type": "string"}}, ["host"]),
    fn("oracle_tablespaces", "Mostra o uso de espaço das tablespaces do banco Oracle."),
    fn("oracle_sessoes", "Mostra quantas sessões estão abertas no banco Oracle e quantas estão ativas."),
    fn("lembrar", "Guarda um fato sobre o Reinaldo para lembrar em conversas futuras. Use quando ele pedir "
       "para lembrar, anotar ou guardar alguma coisa.",
       {"fato": {"type": "string", "description": "o fato, numa frase curta"}}, ["fato"]),
]

FAKE_RESULTS = {
    "calcular": lambda a: _safe_calc(a.get("expressao", "")),
    "zabbix_problemas": lambda a: {"problemas": [
        {"host": "agatha-srv", "severidade": "média", "descricao": "Uso de disco em /dados acima de 80%",
         "desde": "21h02"}]},
    "zabbix_host": lambda a: {"host": a.get("host"), "disponivel": True, "cpu_pct": 23, "memoria_pct": 61,
                              "disco_raiz_pct": 41},
    "oracle_tablespaces": lambda a: {"tablespaces": [
        {"nome": "USERS", "usado_pct": 91}, {"nome": "SYSTEM", "usado_pct": 74}, {"nome": "SYSAUX", "usado_pct": 66}]},
    "oracle_sessoes": lambda a: {"total": 48, "ativas": 5},
    "lembrar": lambda a: {"ok": True},
}


def _safe_calc(expr):
    expr = str(expr)  # o modelo às vezes manda um número em vez de texto
    if not re.fullmatch(r"[\d\s.+\-*/()%,^]+", expr):
        return {"erro": "expressão inválida"}
    try:
        return {"resultado": eval(expr.replace(",", ".").replace("^", "**"), {"__builtins__": {}})}
    except Exception as e:
        return {"erro": str(e)}


class LLMError(Exception):
    pass


# (pergunta, ferramenta esperada ou None, verificação dos argumentos, texto que deve estar na resposta final)
CASES = [
    ("Quanto é 15% de 240?", "calcular", lambda a: _safe_calc(a["expressao"]).get("resultado") == 36, "36"),
    ("Quanto é 1234 vezes 56?", "calcular", lambda a: _safe_calc(a["expressao"]).get("resultado") == 69104, "69"),
    ("Quanto é 2 elevado a 10?", "calcular", lambda a: _safe_calc(a["expressao"]).get("resultado") == 1024, "1024"),
    ("Tenho 1,5 tera usados e cresce 20 gigas por dia. Em quantos dias chego a 2 tera?", "calcular",
     lambda a: abs(float(_safe_calc(a["expressao"]).get("resultado", -1)) - 25) < 1.5, "25"),
    ("Tem algum alerta no Zabbix agora?", "zabbix_problemas", lambda a: True, "disco"),
    ("Como está o host agatha-srv no Zabbix?", "zabbix_host", lambda a: "agatha" in a.get("host", "").lower(), "23"),
    ("Qual o uso de CPU do servidor agatha-srv?", "zabbix_host", lambda a: "agatha" in a.get("host", "").lower(), "23"),
    ("Tem algum problema no host agatha-srv?", "zabbix_problemas", lambda a: True, "disco"),
    ("Como estão as tablespaces do Oracle?", "oracle_tablespaces", lambda a: True, "USERS"),
    ("Tem alguma tablespace quase cheia?", "oracle_tablespaces", lambda a: True, "USERS"),
    ("Quantas sessões tem abertas no banco?", "oracle_sessoes", lambda a: True, "48"),
    ("Lembra que eu uso o padrão ambiente-servidor-host nos hosts do Zabbix.", "lembrar",
     lambda a: "ambiente" in a.get("fato", "").lower(), None),
    ("Anota aí: minha reunião com o time é na quinta às 10h.", "lembrar",
     lambda a: "quinta" in a.get("fato", "").lower(), None),
    ("Guarda pra mim que o banco de produção se chama PRDORA.", "lembrar",
     lambda a: "prdora" in a.get("fato", "").lower(), None),
    ("Oi Agatha, tudo bem?", None, None, None),
    ("O que é um trigger no Zabbix?", None, None, None),
    ("O que é uma tablespace?", None, None, None),
    ("Qual a diferença entre Podman e Docker?", None, None, None),
    ("Que horas são agora?", None, None, None),
    ("Me sugere um filme de ação.", None, None, None),
    ("Obrigado, Agatha.", None, None, None),
    ("Me explica rapidinho como o Zabbix coleta dados.", None, None, None),
]


def stream(messages: list[dict]) -> dict:
    """Uma chamada em streaming; devolve texto, tool_calls e tempos."""
    body = {"messages": messages, "tools": TOOLS, "stream": True, "max_tokens": 200, "temperature": 0.6,
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.perf_counter()
    decision_at = first_chunk_at = None
    text, calls = "", {}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:  # ex.: argumento de ferramenta com JSON degenerado
        return {"text": "", "calls": [], "decision_s": time.perf_counter() - t0, "first_chunk_s": None,
                "total_s": time.perf_counter() - t0, "error": e.read().decode()[:200]}
    with r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            choices = json.loads(line[6:]).get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            now = time.perf_counter() - t0
            if delta.get("tool_calls"):
                decision_at = decision_at or now
                for tc in delta["tool_calls"]:
                    c = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    c["name"] += tc.get("function", {}).get("name") or ""
                    c["arguments"] += tc.get("function", {}).get("arguments") or ""
            if delta.get("content"):
                decision_at = decision_at or now
                text += delta["content"]
                if first_chunk_at is None and re.search(r"[.!?](\s|$)|[,:]\s", text):
                    first_chunk_at = now
    return {"text": text.strip(), "calls": list(calls.values()), "decision_s": decision_at,
            "first_chunk_s": first_chunk_at, "total_s": time.perf_counter() - t0}


EX_NOW = datetime(2026, 10, 10, 9, 0)


def _example_call(question: str, name: str, args: dict, result: dict, answer: str) -> list[dict]:
    return [
        user_message(question, EX_NOW),
        {"role": "assistant", "content": "", "tool_calls": [{"id": f"ex_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": f"ex_{name}", "content": json.dumps(result, ensure_ascii=False)},
        {"role": "assistant", "content": answer},
    ]


# Exemplos fixos no começo da conversa (ficam no cache do prefixo; não custam latência por turno)
FEW_SHOT = (
    _example_call("Quanto é 18% de 350?", "calcular", {"expressao": "350 * 0.18"}, {"resultado": 63.0},
                  "Dá 63.")
    + _example_call("Como está o uso das tablespaces?", "oracle_tablespaces", {},
                    {"tablespaces": [{"nome": "DADOS", "usado_pct": 52}]}, "Tudo tranquilo. A DADOS está com 52%.")
    + _example_call("Lembra que meu notebook se chama rei-note.", "lembrar", {"fato": "O notebook do Reinaldo se chama rei-note"},
                    {"ok": True}, "Pode deixar, guardei.")
    + [user_message("O que é um índice num banco de dados?", EX_NOW),
       {"role": "assistant", "content": "É uma estrutura que acelera as buscas. Funciona como o índice de um livro."},
       user_message("Para que serve um template no Zabbix?", EX_NOW),
       {"role": "assistant", "content": "Ele agrupa itens, triggers e gráficos prontos. Você aplica em vários hosts "
                                        "de uma vez, sem configurar um por um."}]
)


def run_case(question: str, expected: str | None, check, must_contain: str | None, note: str = "v1",
             few_shot: bool = False) -> dict:
    system = {"role": "system", "content": system_message()["content"] + TOOLS_NOTES[note]}
    messages = [system, *(FEW_SHOT if few_shot else []), user_message(question, NOW)]
    first = stream(messages)
    call = first["calls"][0] if first["calls"] else None
    got = call["name"] if call else None
    row = {"question": question, "expected": expected, "got": got, "decision_s": first["decision_s"],
           "first_text": first["text"], "correct_choice": got == expected}
    if call:
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            args = None
        row["arguments"] = args
        row["args_ok"] = bool(args is not None and got == expected and (check is None or _try(check, args)))
        result = FAKE_RESULTS.get(got, lambda a: {"erro": "ferramenta desconhecida"})(args or {})
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": got, "arguments": call["arguments"]}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(result, ensure_ascii=False)},
        ]
        second = stream(messages)
        row.update(answer=second["text"], answer_first_chunk_s=second["first_chunk_s"], answer_calls=second["calls"],
                   uses_result=(must_contain is None or must_contain.lower() in second["text"].lower()))
    return row


def _try(check, args) -> bool:
    try:
        return bool(check(args))
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--note", choices=sorted(TOOLS_NOTES), default="v2", help="versão da nota de ferramentas")
    parser.add_argument("--few-shot", action="store_true", help="exemplos de chamadas no começo da conversa")
    args = parser.parse_args()

    stream([system_message(), user_message("Oi.", NOW)])  # aquecimento do cache
    rows = []
    for run in range(args.runs):
        for case in CASES:
            r = run_case(*case, note=args.note, few_shot=args.few_shot)
            r["run"] = run
            rows.append(r)
            mark = "✅" if r["correct_choice"] and r.get("args_ok", True) and r.get("uses_result", True) else "❌"
            extra = f" args={json.dumps(r.get('arguments'), ensure_ascii=False)}" if r["got"] else ""
            print(f"{mark} {r['question'][:55]:55s} esperado={r['expected'] or '-':18s} "
                  f"escolheu={r['got'] or '-':18s} decisão {r['decision_s']:.2f}s{extra}", flush=True)

    tool_rows = [r for r in rows if r["expected"]]
    plain_rows = [r for r in rows if not r["expected"]]
    called = [r for r in rows if r["got"]]
    print(f"\nescolha certa com ferramenta: {sum(r['correct_choice'] for r in tool_rows)}/{len(tool_rows)}")
    print(f"escolha certa sem ferramenta: {sum(r['correct_choice'] for r in plain_rows)}/{len(plain_rows)}")
    print(f"argumentos certos:            {sum(r.get('args_ok', False) for r in tool_rows)}/{len(tool_rows)}")
    print(f"resposta usou o resultado:    {sum(r.get('uses_result', False) for r in tool_rows)}/{len(tool_rows)}")
    print(f"decisão p50: com ferramenta {statistics.median(r['decision_s'] for r in called):.2f}s · "
          f"sem {statistics.median(r['decision_s'] for r in plain_rows if not r['got']):.2f}s")
    answers = [r["answer_first_chunk_s"] for r in called if r.get("answer_first_chunk_s")]
    if answers:
        print(f"2ª ida (resposta com o resultado), 1º trecho p50: {statistics.median(answers):.2f}s")

    RESULTS_DIR.mkdir(exist_ok=True)
    variant = args.note + ("-fewshot" if args.few_shot else "")
    out = RESULTS_DIR / f"tools-{variant}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "runs": args.runs,
                               "note": TOOLS_NOTES[args.note],
                               "tools": TOOLS, "rows": rows}, ensure_ascii=False, indent=2, default=str))
    print(f"\nresultados: {out}")


if __name__ == "__main__":
    main()
