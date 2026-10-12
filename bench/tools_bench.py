"""Benchmark de chamada de ferramentas (tool calling) do LLM da Agatha (ADR-009).

Usa o llama-server no ar (pod `agatha`, porta 8080 do host) com EXATAMENTE o prompt que o servidor
monta (regras das ferramentas, exemplos e bloco de memória: `conversation.build_prefix`). As
ferramentas são as reais (calculadora, guardar_fato, esquecer, Zabbix, Oracle), com resultados simulados. Para cada pergunta, mede:
- se o modelo decidiu certo: chamar a ferramenta esperada, ou responder direto quando não precisa;
- se os argumentos fazem sentido (verificação por pergunta);
- tempo até a decisão (1º token de texto ou 1º delta de tool_call);
- nas perguntas com ferramenta: a 2ª ida ao LLM com o resultado, e se a resposta o usou;
- nas perguntas sem ferramenta: se a resposta contém (ou não contém) um texto esperado.

As variantes anteriores (sem exemplos, regras v1/v2) estão nos resultados `tools-v*.json`.

Uso:
    .venv/bin/python bench/tools_bench.py --runs 3
"""

import argparse
import dataclasses
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from agatha_server.conversation import build_prefix
from agatha_server.llm import NO_BRACKETS
from agatha_server.memory import MemoryConfig, MemoryStore
from agatha_server.prompts import user_message
from agatha_server.tools import Example, Tool, ToolRegistry
from agatha_server.tools.calc import calculator, evaluate
from agatha_server.tools.memory import memory_tools
from agatha_server.tools.oracle import oracle_tools
from agatha_server.tools.zabbix import ZabbixClient, ZabbixConfig, zabbix_tools

URL = "http://127.0.0.1:8080/v1/chat/completions"
RESULTS_DIR = Path(__file__).parent / "results"
NOW = datetime(2026, 10, 11, 21, 30)

# Memória de teste (os números importam para os casos de "esquecer")
FACTS = [
    "O Reinaldo usa o padrão ambiente-servidor-host nos hosts do Zabbix",  # 1
    "O banco de produção se chama PRDORA",                                  # 2
    "A reunião com o time é na quinta às 10h",                              # 3
]


def fake(result: dict):
    return lambda args: result


_ZABBIX_FAKE = {
    "zabbix_problemas": {"problemas": [{"host": "agatha-srv", "severidade": "média",
                                        "descricao": "Uso de disco em /dados acima de 80%", "desde": "21h02"}]},
    "zabbix_host": {"host": "agatha-srv", "disponivel": True, "cpu_pct": 23, "memoria_pct": 61,
                    "ligado_ha": "8 horas", "disco_pct": {"/dados": 41}},
}
_ORACLE_FAKE = {
    "oracle_tablespaces": {"tablespaces": [{"nome": "USERS", "usado_pct": 91}, {"nome": "SYSTEM", "usado_pct": 74},
                                           {"nome": "SYSAUX", "usado_pct": 66}]},
    "oracle_sessoes": {"total": 48, "ativas": 5},
}
ZABBIX_ORACLE = [
    # Zabbix: definições reais do servidor (esquema, regra, exemplo), com resultado simulado
    *[dataclasses.replace(t, handler=fake(_ZABBIX_FAKE[t.name]))
      for t in zabbix_tools(ZabbixClient(ZabbixConfig(url="http://x", token="x")))],
    # Oracle: definições reais do servidor, com resultado simulado
    *[dataclasses.replace(t, handler=fake(_ORACLE_FAKE[t.name])) for t in oracle_tools(None)],
]


def build_registry() -> tuple[ToolRegistry, MemoryStore]:
    store = MemoryStore(MemoryConfig(db=":memory:"))
    for f in FACTS:
        store.add(f)
    # guardar_fato/esquecer com esquema, regra e exemplo reais, mas sem alterar a memória de teste
    mem = [dataclasses.replace(t, handler=fake({"ok": True, "numero": 4} if t.name == "guardar_fato"
                                               else {"ok": True})) for t in memory_tools(store)]
    return ToolRegistry([calculator, *ZABBIX_ORACLE, *mem]), store


def calc_is(value):
    return lambda a: abs(float(evaluate(a["expressao"])) - value) < 0.01


# (pergunta, ferramenta esperada ou None, verificação dos argumentos, texto que deve aparecer na resposta,
#  texto que NÃO pode aparecer na resposta)
CASES = [
    ("Quanto é 15% de 240?", "calcular", calc_is(36), "36", None),
    ("Quanto é 1234 vezes 56?", "calcular", calc_is(69104), "69", None),
    ("Quanto é 2 elevado a 10?", "calcular", calc_is(1024), "1024", None),
    ("Tenho 1,5 tera usados e cresce 20 gigas por dia. Em quantos dias chego a 2 tera?", "calcular",
     lambda a: 24 <= float(evaluate(a["expressao"])) <= 26.5, "2", None),
    ("Tem algum alerta no Zabbix agora?", "zabbix_problemas", lambda a: True, "disco", None),
    ("Como está o host agatha-srv no Zabbix?", "zabbix_host", lambda a: "agatha" in a.get("host", "").lower(), "23", None),
    ("Qual o uso de CPU do servidor agatha-srv?", "zabbix_host", lambda a: "agatha" in a.get("host", "").lower(), "23", None),
    ("Como estão as tablespaces do Oracle?", "oracle_tablespaces", lambda a: True, "USERS", None),
    ("Tem alguma tablespace quase cheia?", "oracle_tablespaces", lambda a: True, "USERS", None),
    ("Quantas sessões tem abertas no banco?", "oracle_sessoes", lambda a: True, "48", None),
    ("Lembra que o meu servidor de testes se chama lab01.", "guardar_fato", lambda a: "lab01" in a.get("fato", "").lower(), None, None),
    ("Anota aí: o backup roda todo dia às 2h.", "guardar_fato", lambda a: "backup" in a.get("fato", "").lower(), None, None),
    ("Esquece que o banco de produção se chama PRDORA.", "esquecer", lambda a: int(a.get("numero")) == 2, None, None),
    ("Pode apagar aquilo da reunião.", "esquecer", lambda a: int(a.get("numero")) == 3, None, None),
    ("Oi Agatha, tudo bem?", None, None, None, None),
    ("O que é um trigger no Zabbix?", None, None, None, "não tenho"),
    ("O que é uma tablespace?", None, None, None, None),
    ("Qual a diferença entre Podman e Docker?", None, None, None, None),
    ("Que horas são agora?", None, None, "21h30", None),
    ("Me sugere um filme de ação.", None, None, None, None),
    ("Obrigado, Agatha.", None, None, None, None),
    ("O que você sabe sobre mim?", None, None, "PRDORA", None),
    ("Qual o padrão de nome que eu uso nos hosts?", None, None, "ambiente", None),
    ("Qual o nome do meu notebook?", None, None, None, "rei-note"),  # só existe no exemplo do prompt
]


def stream(messages: list[dict], tools: list[dict]) -> dict:
    body = {"messages": messages, "tools": tools, "parallel_tool_calls": False, "stream": True, "max_tokens": 200,
            "temperature": 0.6, "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
            "logit_bias": NO_BRACKETS}
    t0 = time.perf_counter()
    decision_at = first_chunk_at = None
    text, calls = "", {}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:  # ex.: argumento de ferramenta com JSON degenerado
        return {"text": "", "calls": [], "decision_s": time.perf_counter() - t0, "first_chunk_s": None,
                "error": e.read().decode()[:200]}
    with r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            choices = json.loads(line[6:]).get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            now = time.perf_counter() - t0
            for tc in delta.get("tool_calls") or []:
                decision_at = decision_at or now
                c = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                c["name"] += tc.get("function", {}).get("name") or ""
                c["arguments"] += tc.get("function", {}).get("arguments") or ""
            if delta.get("content"):
                decision_at = decision_at or now
                text += delta["content"]
                if first_chunk_at is None and re.search(r"[.!?](\s|$)|[,:]\s", text):
                    first_chunk_at = now
    return {"text": text.strip(), "calls": list(calls.values()), "decision_s": decision_at, "first_chunk_s": first_chunk_at}


def _try(check, args) -> bool:
    try:
        return bool(check(args))
    except Exception:
        return False


def run_case(registry, prefix, question, expected, check, must_contain, must_not_contain) -> dict:
    schemas = registry.schemas()
    messages = [*prefix, user_message(question, NOW)]
    first = stream(messages, schemas)
    call = first["calls"][0] if first["calls"] else None
    got = call["name"] if call else None
    row = {"question": question, "expected": expected, "got": got, "decision_s": first["decision_s"],
           "first_text": first["text"], "error": first.get("error"), "correct_choice": got == expected}
    final_text = first["text"]
    if call:
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            args = None
        row["arguments"] = args
        row["args_ok"] = bool(args is not None and got == expected and (check is None or _try(check, args)))
        tool = registry.tools.get(got)
        result = tool.handler(args or {}) if tool else {"erro": "ferramenta desconhecida"}
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": got, "arguments": call["arguments"]}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(result, ensure_ascii=False)},
        ]
        second = stream(messages, schemas)
        final_text = second["text"]
        row.update(answer=second["text"], answer_first_chunk_s=second["first_chunk_s"])
    lower = final_text.lower()
    row["text_ok"] = ((must_contain is None or must_contain.lower() in lower)
                      and (must_not_contain is None or must_not_contain.lower() not in lower))
    row["ok"] = row["correct_choice"] and row.get("args_ok", True) and row["text_ok"]
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    registry, store = build_registry()
    prefix = build_prefix(registry, store)
    stream([*prefix, user_message("Oi.", NOW)], registry.schemas())  # aquecimento do cache

    rows = []
    for run in range(args.runs):
        for case in CASES:
            r = run_case(registry, prefix, *case)
            r["run"] = run
            rows.append(r)
            extra = f" args={json.dumps(r.get('arguments'), ensure_ascii=False)}" if r["got"] else ""
            print(f"{'✅' if r['ok'] else '❌'} {r['question'][:52]:52s} esperado={r['expected'] or '-':18s} "
                  f"escolheu={r['got'] or '-':18s} {r['decision_s']:.2f}s{extra}", flush=True)
            if not r["ok"]:
                print(f"     resposta: {(r.get('answer') or r['first_text'])[:110]!r}", flush=True)

    tool_rows = [r for r in rows if r["expected"]]
    plain_rows = [r for r in rows if not r["expected"]]
    called = [r for r in rows if r["got"]]
    print(f"\nescolha certa com ferramenta: {sum(r['correct_choice'] for r in tool_rows)}/{len(tool_rows)}")
    print(f"escolha certa sem ferramenta: {sum(r['correct_choice'] for r in plain_rows)}/{len(plain_rows)}")
    print(f"argumentos certos:            {sum(r.get('args_ok', False) for r in tool_rows)}/{len(tool_rows)}")
    print(f"resposta com o conteúdo certo: {sum(r['text_ok'] for r in rows)}/{len(rows)}")
    print(f"tudo certo:                    {sum(r['ok'] for r in rows)}/{len(rows)}")
    if called:
        print(f"decisão p50 com ferramenta: {statistics.median(r['decision_s'] for r in called):.2f}s")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"tools-servidor-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "runs": args.runs,
                               "system_prompt": prefix[0]["content"], "tools": registry.schemas(), "rows": rows},
                              ensure_ascii=False, indent=2, default=str))
    print(f"\nresultados: {out}")


if __name__ == "__main__":
    main()
