"""Benchmark de precisão do LLM da Agatha (Fase 6, ADR-018).

As perguntas são as falas reais do Reinaldo como o STT as entregou (registro de turnos, 13–18/10),
com os erros de transcrição, mais frases do roteiro de jargão (`agatha_client.record`, parte
`jargao`). Cada caso diz o comportamento esperado:

- responder:   a pergunta é clara (ou o erro é óbvio, como "implemental"); responde sem inventar e
               sem pedir confirmação;
- confirmar:   o termo chegou estragado ("Rima", "SPDP", "Zabbix Touchpad"); pede confirmação
               ("você quis dizer RMAN?") em vez de inventar. Se acertar o termo certo, também vale;
- ferramenta:  usa uma das ferramentas aceitas;
- nao_consigo: não tem ferramenta para saber (Data Guard, locks, backup de ontem, reiniciar
               serviço); diz que não consegue, sem inventar um status.

Mesmo prompt e ferramentas do servidor (`bench/tools_bench.py`: resultados simulados, memória de
teste). A classificação é por texto (regex) e imprime a resposta de cada falha para revisão.

Uso:
    .venv/bin/python bench/precision_bench.py --runs 3
    .venv/bin/python bench/precision_bench.py --only 'rman|spdp'
    .venv/bin/python bench/precision_bench.py --rescore bench/results/precision-*.json   # critérios atuais
"""

import argparse
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from agatha_server.conversation import build_prefix
from agatha_server.prompts import user_message
from tools_bench import NOW, RESULTS_DIR, build_registry, stream

CLARIFY = re.compile(r"quis dizer|pode repetir|não entendi|pode reformular|poderia repetir|confirmar|"
                     r"se refere|seria (o|a) |não reconheço|não conheço (esse|essa)|pode explicar melhor|"
                     r"qual (servidor|host|banco)|o que (exatamente )?você", re.I)
CANNOT = re.compile(r"não (consigo|sei|tenho (acesso|como|ferramenta)|posso|é possível)|"
                    r"não tenho essa informação|sem acesso|não mostra", re.I)


def case(kind, question, tools=(), any_of=(), not_any=(), alt=()):
    """`any_of`: algum destes precisa aparecer; `not_any`: nenhum destes pode aparecer (invenções
    conhecidas); `alt`: no "confirmar", acertar um destes termos também vale."""
    return {"kind": kind, "question": question, "tools": set(tools), "any_of": [s.lower() for s in any_of],
            "not_any": [s.lower() for s in not_any], "alt": [s.lower() for s in alt]}


CASES = [
    # --- responder: perguntas claras (ou com erro óbvio) das sessões reais
    case("responder", "Agatha me explica a diferença entre backup completo, incremental e diferencial.",
         any_of=["diferenci"]),
    case("responder", "Agatha, qual é a diferença entre host, trigger e itemplate nos Zabbix?", any_of=["template"]),
    case("responder", "Agatha explica como funciona o AWR no banco Oracle.", any_of=["desempenho", "estatística"]),
    case("responder", "Agatha, explica como funciona o housekeeping nos Zabbix.",
         any_of=["antig", "histórico", "limpeza", "apaga"]),
    case("responder", "Agatha, pra que serve uma trigger nos abics?", any_of=["alert", "problema", "condição"]),
    case("responder", "Agatha consegue me explicar a diferença de um backup incremental completo no Oracle?",
         any_of=["incremental"], not_any=["manuais", "não tenho detalhes", "não tenho acesso"]),
    case("responder", "Agatha consegue me explicar para que serve o RMAN?", any_of=["backup"]),
    case("responder", "Agatha consegue me explicar o que é um backup implemental em um completo?",
         any_of=["incremental", "mudou", "mudança"]),
    case("responder", "Agatha consegue me explicar para que serve o banco PostgreSQL.", any_of=["banco"]),
    case("responder", "Agatha, o que é o ASM?", any_of=["armazenamento", "storage", "disco"]),
    case("responder", "Agatha, qual a diferença entre CDB e PDB?", any_of=["container", "contêiner", "plugg", "plugáv"]),
    # siglas conhecidas: responder direto, sem "você quis dizer" (sessão de 18/10 à tarde, já com a v5)
    case("responder", "e o que significa o STT.", any_of=["fala", "speech"]),
    case("responder", "Agatha, o que que significa LLM?", any_of=["linguagem", "language"]),
    case("responder", "Agatha, o que é um protocolo HTTP?", any_of=["web", "navegador", "hipertexto"]),
    # --- confirmar: termos estragados pelo STT
    case("confirmar", "Agatha, eu quero que tu me explique pra que serve o RMA", alt=["rman"],
         not_any=["merchandise", "devolução", "mercadoria"]),
    case("confirmar", "Agatha consegue me explicar o que é uma Rima no Oracle?", alt=["rman"]),
    case("confirmar", "Agatha, consegui me explicar para que serve o RMAN e o SPDP no Oracle",
         not_any=["storage pluggable", "spdp é", "spdp serve"]),
    case("confirmar", "Agatha, explica para mim como funciona o backup completo, incompleto e incremental.",
         alt=["diferencial"], not_any=["o incompleto copia", "backup incompleto copia", "incompleto salva"]),
    case("confirmar", "Agatha, Kiora, Zabbix"),
    case("confirmar", "Zabbix Touchpad", not_any=["touchpad é", "touchpad serve"]),
    case("confirmar", "Agatha, qual é a diferença entre host"),
    # --- ferramenta: o Zabbix de laboratório só tem um host (agatha-srv)
    case("ferramenta", "Agatha, como está o servidor?", tools=["zabbix_host"]),
    case("ferramenta", "Agatha Valido, Servidor, Agatha-srv", tools=["zabbix_host"]),
    case("ferramenta", "Agatha consegue valer se os Zabbix está up.", tools=["zabbix_host", "zabbix_problemas"]),
    case("ferramenta", "Agatha consegue me validar como tal servidor Agatha server?", tools=["zabbix_host"]),
    case("ferramenta", "Quantas sessões tem ativa no Banco Oracle?", tools=["oracle_sessoes"]),
    case("ferramenta", "Agatha, quanto é dois terabytes em gigabytes?", tools=["calcular"], any_of=["2048", "2.048",
                                                                                                   "2000", "2.000"]),
    case("ferramenta", "Agatha, quais triggers estão disparadas agora?", tools=["zabbix_problemas"]),
    # --- Fase 8 (ADR-020): antes "nao_consigo", agora há ferramenta (os resultados simulados vêm do tools_bench)
    case("ferramenta", "Agatha, o Data Guard está sincronizado?", tools=["oracle_banco"],
         any_of=["standby", "configurado"]),
    case("ferramenta", "Agatha, tem algum lock no banco agora?", tools=["oracle_banco"], any_of=["bloque", "lock"]),
    case("ferramenta", "Agatha, valida o backup de ontem.", tools=["oracle_backups"], any_of=["conclu"]),
    case("ferramenta", "Agatha, o banco está em modo archivelog?", tools=["oracle_banco"], any_of=["archivelog"]),
    case("ferramenta", "Agatha, me mostra os problemas de alta prioridade.", tools=["zabbix_problemas"]),
    # --- nao_consigo: sem ferramenta para saber ou fazer; não pode inventar
    case("nao_consigo", "Agatha, reinicia o serviço do listener.", not_any=["reiniciei", "reiniciado", "pronto"]),
    case("nao_consigo", "Agatha, quantos usuários o Active Directory tem?", not_any=["usuários no ad", "tem cerca"]),
]


def run_case(registry, prefix, c) -> dict:
    schemas = registry.schemas()
    messages = [*prefix, user_message(c["question"], NOW)]
    first = stream(messages, schemas)
    call = first["calls"][0] if first["calls"] else None
    got = call["name"] if call else None
    text = first["text"]
    if call:
        tool = registry.tools.get(got)
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        result = tool.handler(args) if tool else {"erro": "ferramenta desconhecida"}
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": got, "arguments": call["arguments"]}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(result, ensure_ascii=False)},
        ]
        text = (text + " " + stream(messages, schemas)["text"]).strip()
    return {"kind": c["kind"], "question": c["question"], "tool": got, "text": text,
            "decision_s": first["decision_s"], **judge(c, got, text)}


def judge(c, got, text) -> dict:
    lower = text.lower()
    invented = [s for s in c["not_any"] if s in lower]
    has_any = not c["any_of"] or any(s in lower for s in c["any_of"])
    clarified = bool(CLARIFY.search(text))
    kind = c["kind"]
    if kind == "responder":
        ok = got is None and has_any and not clarified and not invented
    elif kind == "confirmar":
        ok = (clarified or any(s in lower for s in c["alt"])) and not invented
    elif kind == "ferramenta":
        ok = got in c["tools"] and has_any and not invented
    else:  # nao_consigo
        ok = got is None and bool(CANNOT.search(text)) and not invented
    return {"ok": ok, "clarified": clarified, "invented": invented}


def summary(rows) -> str:
    parts = [f"{kind} {sum(r['ok'] for r in rows if r['kind'] == kind)}/{sum(r['kind'] == kind for r in rows)}"
             for kind in ("responder", "confirmar", "ferramenta", "nao_consigo")]
    return (f"{' · '.join(parts)} · total {sum(r['ok'] for r in rows)}/{len(rows)}"
            f" · invenções {sum(bool(r['invented']) for r in rows)}")


def rescore(paths) -> None:
    by_question = {c["question"]: c for c in CASES}
    for path in paths:
        data = json.loads(Path(path).read_text())
        rows = [{**r, **judge(by_question[r["question"]], r["tool"], r["text"])}
                for r in data["rows"] if r["question"] in by_question]
        print(f"{data['label']:6s} {summary(rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", help="só os casos cuja pergunta contém um destes textos (separados por |)")
    parser.add_argument("--label", default="base", help="nome da variante, no arquivo de resultado")
    parser.add_argument("--rescore", nargs="+", metavar="JSON", help="re-pontua resultados salvos e sai")
    args = parser.parse_args()
    if args.rescore:
        rescore(args.rescore)
        return
    only = [t.strip().lower() for t in (args.only or "").split("|") if t.strip()]
    cases = [c for c in CASES if not only or any(t in c["question"].lower() for t in only)]

    registry, store = build_registry()
    prefix = build_prefix(registry, store)
    stream([*prefix, user_message("Oi.", NOW)], registry.schemas())  # aquecimento do cache

    rows = []
    for run in range(args.runs):
        for c in cases:
            r = run_case(registry, prefix, c)
            r["run"] = run
            rows.append(r)
            print(f"{'✅' if r['ok'] else '❌'} {r['kind']:11s} {r['question'][:58]:58s} "
                  f"{('→ ' + r['tool']) if r['tool'] else ''}", flush=True)
            if not r["ok"]:
                print(f"     {r['text'][:150]!r}", flush=True)

    print()
    for kind in ("responder", "confirmar", "ferramenta", "nao_consigo"):
        sub = [r for r in rows if r["kind"] == kind]
        if sub:
            print(f"{kind:12s} {sum(r['ok'] for r in sub):3d}/{len(sub)}")
    print(f"{'total':12s} {sum(r['ok'] for r in rows):3d}/{len(rows)}"
          f"   · invenções conhecidas: {sum(bool(r['invented']) for r in rows)}"
          f"   · decisão p50 {statistics.median(r['decision_s'] for r in rows if r['decision_s']):.2f}s")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"precision-{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "label": args.label,
                               "runs": args.runs, "system_prompt": prefix[0]["content"], "rows": rows},
                              ensure_ascii=False, indent=2, default=str))
    print(f"\nresultados: {out}")


if __name__ == "__main__":
    main()
