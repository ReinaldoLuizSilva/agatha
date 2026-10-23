# ADR-026: Busca na documentação — trecho da página em vez do resumo do buscador

- **Status:** Aceito
- **Data:** 2026-10-22
- **Fase:** 11, item 2 (trechos melhores na busca; escopo aprovado em 22/10)
- **Complementa:** [ADR-025](0025-busca-na-documentacao.md) (busca na documentação oficial com SearXNG)

## Contexto

No teste da busca no Windows (22/10, ADR-025), os trechos vinham do resumo do buscador (`content`
do SearXNG). Eles quase sempre falam do assunto, mas muitas vezes sem a resposta: na porta padrão do
Zabbix o trecho não trazia o número e ela completou de memória ("10051 para a interface web",
errado); no PostgreSQL 17 os trechos eram do índice das notas de versão e a resposta ficou genérica.
O ADR-025 tinha descartado "baixar e ler a página" por custar 1–2 s e por trazer texto demais para o
modelo de 4B. A ideia aqui é baixar a página, sim, mas entregar só um pedaço curto dela.

## Decisão

1. **Baixar as páginas dos até 3 resultados em paralelo**
   ([`tools/search.py`](../../server/agatha_server/tools/search.py), `SearchClient._passage`), com
   **prazo de 1,5 s para cada página inteira** (`asyncio.wait_for`, porque o timeout do httpx vale
   por operação e uma página lenta passaria disso), no máximo 1,5 MB e só HTML. Se a página falhar
   ou não tiver os termos da consulta, aquele resultado fica com o resumo do buscador, como antes.
2. **Trecho de ~300 caracteres** (`passage`): o HTML vira blocos de texto (parágrafos, itens, linhas
   de tabela, código), sem `script`, `nav`, `header`, `footer`, `aside` e formulários. O bloco
   escolhido é o que tem mais termos da consulta (peso 3) e do resumo do buscador (peso 4 × fração),
   com uma penalidade pequena para blocos longos. O nome do produto não conta, porque está na página
   inteira. Os termos em português mais comuns da consulta ganham a versão em inglês ("porta" →
   "port", "padrão" → "default"), já que a documentação costuma estar em inglês. O trecho cresce
   para os dois lados até 300 caracteres, para pegar o bloco de código vizinho (`(PORT=1521)` vem
   antes do parágrafo), e é cortado a partir de um pouco antes do 1º termo da consulta.
3. O parse roda numa thread (`asyncio.to_thread`, ~50 ms para 500 KB), para não atrasar o áudio da
   frase de espera.
4. **Ligado por padrão**; `AGATHA_BUSCA_PAGINAS=0` no `server/.env` volta ao resumo do buscador.

## Resultados

[`bench/search_bench.py`](../../bench/search_bench.py): 14 perguntas técnicas com resposta conhecida
(portas padrão, archivelog, DB_BLOCK_SIZE, DNSOption do Quadlet, RMAN, kubectl, MemorySwapMax,
ProxyMode, restart policy, housekeeping, PostgreSQL 17), com um SearXNG temporário igual ao do pod e
o LLM do pod. O resultado é entregue como na conversa (chamada de ferramenta já respondida). A régua
procura o texto exato da resposta.

| 22/10, 21:49–21:51 | resumo do buscador | trecho da página |
|---|---|---|
| resposta no que vai ao LLM | 5/14 | **9/14** |
| resposta da Agatha com o texto certo | 4/14 | **8/14** |
| busca, p50 / máx | 0,48 / 0,56 s | 1,45 / 2,02 s |
| tamanho do resultado, p50 | 822 caracteres | 910 caracteres |
| 1º pedaço da resposta do LLM, p50 | 1,92 s | 2,11 s |

- Ganhos com a página: DB_BLOCK_SIZE (8192), Oracle listener (1521), ProxyMode do proxy Zabbix,
  período do housekeeping. O kubectl entrou no resultado, mas a resposta não repetiu o comando inteiro.
- Erros que continuam: a página do trapper não traz 10051; archivelog ("ALTER SYSTEM", a página
  escolhida era a de arquivamento, não a do comando); PostgreSQL 17 (falou do `COPY ... ON_ERROR`,
  que é novidade da 17 de verdade, mas a régua procura outros termos). Em DNSOption e MemorySwapMax a
  resposta estava certa, mas sem o texto literal que a régua procura.
- Na medição anterior (21:12, antes do prazo por página) foram 8/14 e 7/14, com máximo de 2,43 s.
  Também foram testados, sem arquivo salvo, trechos maiores (não acertaram mais e o LLM processa
  ~200 tokens/s) e um peso maior para o resumo do buscador (piorou, 6–9 de 14).
- No ar (de dentro do pod): na 1ª busca, as 3 páginas do docs.oracle.com caíram juntas aos 1,0 s
  (`ConnectTimeout`). Era o DNS do pod, que perde consultas e só tenta de novo depois de 1 s
  (ADR-022), e o timeout de conexão do httpx (1 s) inclui resolver o nome. Com o cache do DNS quente,
  as páginas chegavam em ~0,1–0,3 s. A conexão passou a usar o prazo inteiro da página (1,5 s), então
  uma consulta perdida ainda deixa ~0,5 s para baixar a página.
- Testes: `server/tests/test_search.py` (trecho em volta do assunto sem menus nem scripts, página
  sem os termos, página que falha fica com o resumo).

### Teste no Windows (23/10, headset, 15 falas)

- Certas com a página: trapper 10051 (no bench errava; agora a página do trapper trouxe "10051 por
  padrão"), listener 1521 ("listrador" no STT), ProxyMode, housekeeping. DB_BLOCK_SIZE: "tipicamente
  4096 ou 8192" (o trecho era o dos valores típicos, não o do padrão).
- Porta padrão do Zabbix agent: o STT entregou "dos Zabbix, a gente". Ela acertou 10050 para o agente
  e disse "80 ou 443 para a interface web", o que não está errado. O "10051 para a interface web" de
  22/10 não voltou.
- PostgreSQL 17 continua genérico ("melhorias no otimizador"). As 3 páginas do postgresql.org estouraram
  o prazo: de dentro do pod elas levam 0,65–1,5 s (servidores no exterior), ou 2,2 s com uma consulta
  de DNS perdida. O prazo ficou como está, porque no bench, com as páginas, a resposta também não saía.
- "Procure **uma** documentação como ativar o modo archive log" não buscou: o STT trocou o "na". Sem a
  busca, ela inventou (`LOG_ARCHIVE_DEST_2` e reiniciar o banco). Corrigido: "uma/a documentação"
  também vale. Nas 564 falas já registradas, a regra nova só muda essa.
- Tempos com busca: o 1º som segue em 1,1–1,2 s (frase de espera). A busca levou 1,1–2,4 s (no bench
  do host, 1,45 s) e o 1º token do LLM chegou aos 4,1–5,3 s (em 22/10, sem as páginas, ~3,2 s): depois
  da frase de espera ficam ~2,5–3,5 s de silêncio.
- Sem busca, como esperado: RMAN, clima, notícias e "restart policy" (o STT entregou "restart
  policies on list, stop it" e ela confirmou o termo).

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Ficar com o resumo do buscador | 5/14; ela completa de memória quando o trecho não tem a resposta |
| Página inteira, ou trechos grandes, para o LLM | contexto de 6144 e ~200 tokens/s: segundos a mais, e o 4B se perde em texto longo; trechos maiores não acertaram mais |
| Biblioteca de extração (trafilatura, readability) | dependência a mais para o mesmo resultado nos casos medidos; o `HTMLParser` da biblioteca padrão basta |
| Embeddings para achar o trecho | modelo a mais na RAM ou na VRAM (sobra ~0,4 GB) por um ganho não medido |

## Consequências

**Positivas**

- A resposta certa quase dobra (4 → 8 de 14), e o resultado entregue ao LLM fica do mesmo tamanho
  (~900 caracteres). O cache e o histórico não mudam (a busca continua fora do histórico, ADR-025).

**Negativas / riscos**

- **+1 s na busca** (p50 1,45 s, máx ~2 s). O 1º som não muda (a frase de espera sai antes, ~1 s), mas
  a resposta depois da frase de espera chega ~1 s mais tarde.
- Agora o servidor baixa páginas dos sites oficiais (só as URLs dos resultados; nenhum dado do
  Reinaldo vai junto), com um User-Agent próprio. Algum site pode bloquear; nesse caso fica o resumo.
- A escolha do trecho é heurística e calibrada em 14 perguntas: acompanhar no uso as respostas com
  busca (`busca '...' →` no `server.log`).
