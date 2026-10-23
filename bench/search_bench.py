"""Benchmark da busca na documentação (ADR-025, ADR-026): a resposta aparece no que a Agatha recebe?

Para cada pergunta técnica com resposta conhecida: busca no SearXNG, baixa as páginas e mede
- se a resposta está no resumo do buscador (como era até a Fase 10);
- se está no resultado que vai ao LLM (trecho da página, ou o resumo se a página não ajudar);
- se a resposta do LLM (llama-server do pod, porta 8080) a contém, com o resultado entregue como na
  conversa (chamada de ferramenta já respondida);
- o tempo da busca com as páginas.

O SearXNG do pod só ouve dentro do pod: para rodar daqui, suba um temporário com a mesma configuração
(e remova no fim). Espere uns segundos entre rodadas: os buscadores limitam rajadas.

    podman run -d --name searxng-bench -p 127.0.0.1:8889:8888 -e GRANIAN_PORT=8888 \\
        -e SEARXNG_SECRET=$(openssl rand -hex 32) \\
        -v ~/.config/agatha/searxng/settings.yml:/etc/searxng/settings.yml:ro,Z \\
        docker.io/searxng/searxng:2026.10.19-81371861f
    .venv/bin/python bench/search_bench.py --url http://127.0.0.1:8889
    podman rm -f searxng-bench

Os tempos daqui incluem o DNS do host, que perde consultas (ADR-022: 5 s); no pod o timeout é 1 s.
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from agatha_server.conversation import build_prefix
from agatha_server.prompts import user_message
from agatha_server.tools.search import SearchClient, SearchConfig, pick, site_for
from tools_bench import NOW, RESULTS_DIR, build_registry, stream

# (consulta como sai da fala, trechos aceitos como resposta)
CASES = [
    ("Zabbix agent porta padrão", ["10050"]),
    ("Zabbix server porta padrão do trapper", ["10051"]),
    ("PostgreSQL porta padrão", ["5432"]),
    ("Oracle listener porta padrão", ["1521"]),
    ("Oracle como ativar o modo archivelog", ["ALTER DATABASE ARCHIVELOG"]),
    ("o que mudou no PostgreSQL 17", ["incremental", "JSON_TABLE", "MERGE", "vacuum"]),
    ("Oracle tamanho padrão do DB_BLOCK_SIZE", ["8192", "8 KB", "8K"]),
    ("podman quadlet DNSOption", ["--dns-option", "custom DNS options"]),
    ("RMAN backup incremental cumulativo e diferencial", ["cumulative", "differential", "cumulativo", "diferencial"]),
    ("kubectl rollout restart", ["kubectl rollout restart deployment"]),
    ("systemd MemorySwapMax", ["MemorySwapMax="]),
    ("Zabbix proxy modo ativo e passivo parâmetro", ["ProxyMode"]),
    ("docker restart policy unless-stopped", ["unless-stopped"]),
    ("Zabbix housekeeping período de histórico", ["storage period", "período de armazenamento", "quantos dias",
                                                  "history period"]),
]


def has(text: str, answers: list[str]) -> bool:
    return any(a.lower() in text.lower() for a in answers)


async def run(url: str, pause: float, with_llm: bool, pages: bool) -> list[dict]:
    client = SearchClient(SearchConfig(url=url, pages=pages))
    registry, store = build_registry()
    prefix = build_prefix(registry, store)
    rows = []
    try:
        for i, (query, answers) in enumerate(CASES):
            if i:
                await asyncio.sleep(pause)
            site = site_for(query)
            raw = (await client._client.get("/search", params={
                "q": f"{query} site:{site}" if site else query, "format": "json", "language": "pt-BR"})).json()
            snippets = " ".join(r.get("content") or "" for r in pick(raw.get("results") or [], site)[:3])
            t0 = time.perf_counter()
            result = await client.search(query)
            search_s = time.perf_counter() - t0
            text = json.dumps(result, ensure_ascii=False)
            row = {"query": query, "snippet_ok": has(snippets, answers), "result_ok": has(text, answers),
                   "search_s": search_s, "result_chars": len(text), "result": result}
            if with_llm:
                messages = [*prefix, user_message(f"Pesquisa na documentação: {query}.", NOW),
                            {"role": "assistant", "content": "", "tool_calls": [{"id": "busca", "type": "function",
                             "function": {"name": "buscar_documentacao",
                                          "arguments": json.dumps({"consulta": query}, ensure_ascii=False)}}]},
                            {"role": "tool", "tool_call_id": "busca", "content": text}]
                answer = await asyncio.to_thread(stream, messages, registry.schemas())
                row.update(answer=answer["text"], answer_ok=has(answer["text"], answers),
                           answer_first_chunk_s=answer["first_chunk_s"])
            rows.append(row)
            print(f"{'✅' if row['snippet_ok'] else '❌'} resumo  {'✅' if row['result_ok'] else '❌'} resultado  "
                  + (f"{'✅' if row.get('answer_ok') else '❌'} resposta  " if with_llm else "")
                  + f"{search_s:4.1f} s  {len(text):4d} car.  {query}", flush=True)
            if with_llm and not row["answer_ok"]:
                print(f"      {row['answer'][:140]!r}", flush=True)
    finally:
        await client.aclose()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8889", help="SearXNG (veja acima)")
    parser.add_argument("--pause", type=float, default=5.0, help="segundos entre buscas (limite dos buscadores)")
    parser.add_argument("--no-llm", action="store_true", help="só a busca, sem perguntar ao LLM")
    parser.add_argument("--sem-paginas", action="store_true", help="só o resumo do buscador (como na Fase 10)")
    args = parser.parse_args()
    rows = asyncio.run(run(args.url, args.pause, not args.no_llm, not args.sem_paginas))
    n = len(rows)
    print(f"\nresposta no resumo do buscador: {sum(r['snippet_ok'] for r in rows)}/{n}")
    print(f"resposta no resultado entregue: {sum(r['result_ok'] for r in rows)}/{n}")
    if not args.no_llm:
        print(f"resposta certa do LLM:          {sum(r['answer_ok'] for r in rows)}/{n}")
    print(f"busca com páginas p50 {statistics.median(r['search_s'] for r in rows):.2f} s, "
          f"máx {max(r['search_s'] for r in rows):.2f} s; resultado p50 "
          f"{statistics.median(r['result_chars'] for r in rows):.0f} caracteres")
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"search-{'resumo' if args.sem_paginas else 'paginas'}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "rows": rows},
                              ensure_ascii=False, indent=2, default=str))
    print(f"resultados: {out}")


if __name__ == "__main__":
    main()
