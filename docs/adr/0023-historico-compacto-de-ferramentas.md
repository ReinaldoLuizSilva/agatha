# ADR-023: Resultado grande de ferramenta vira marcador no histórico

- **Status:** Aceito
- **Data:** 2026-10-21
- **Complementa:** [ADR-007](0007-ajustes-de-latencia.md) (o prefixo não muda entre turnos) e
  [ADR-009](0009-tool-calling.md) (ferramentas)

## Contexto

O histórico guardava cada troca exatamente como foi ao LLM, inclusive o resultado das ferramentas,
para o cache do llama.cpp continuar valendo (ADR-007). No teste do clima no Windows (21/10, ADR-022),
5 perguntas de clima somaram ~230–280 tokens cada, e na 9ª fala o prompt chegou a 4076 tokens: o
contexto (4096) estourou, o servidor cortou o histórico para 2 trocas e a resposta levou 6,8 s.
As notícias e a busca na web (Fase 10) vão devolver resultados maiores que o clima.

Opções apresentadas ao Reinaldo: **A)** compactar as trocas com ferramenta no histórico; **B)**
aumentar o contexto para 6144 (a GPU tem ~0,4 GB livres: precisa medir com o pod parado); **C)**
guardar menos trocas (12 → 8). Ele escolheu A, sabendo que com o tempo pode precisar de B.

## Decisão

1. Ao guardar uma troca (`Conversation._remember`), o resultado de ferramenta com mais de 150
   caracteres vira `{"omitido": "já usado na resposta; consulte de novo se precisar"}`. A chamada
   da ferramenta e a resposta falada ficam.
2. **A chamada fica** de propósito: só com pergunta e resposta, o histórico viraria um exemplo de
   "pergunta de clima → resposta sem consultar", e o modelo pequeno imita o que vê.
3. **Resultados pequenos ficam inteiros** (`calcular`, `guardar_fato`, `esquecer`): o "esquece
   isso" logo depois de guardar usa o número que o `guardar_fato` devolveu.
4. Na hora da resposta nada muda: o modelo vê o resultado completo. Só o que fica para os turnos
   seguintes é compacto.

## Resultados

- Cache do llama.cpp (`timings.prompt_n`, pergunta seguinte a uma troca de clima): 85 tokens
  reprocessados com o marcador contra 67 sem (574 ms contra 524 ms). Com sliding window attention
  ele refaz só o fim do último turno.
- Cada troca de clima no histórico: ~83 tokens a menos (o resultado já compacto do ADR-022); as de
  Oracle e GitHub, mais.
- Comportamento (`tools_bench`, casos novos com o marcador no histórico, 3 rodadas): depois de uma
  troca compactada, a pergunta seguinte consulta de novo em 18 de 18 ("Vai chover amanhã?", "E as
  sessões?", "Qual a mais cheia agora?", "E em Curitiba?"), e "O que é uma tablespace?" continua sem
  ferramenta. As 2 falhas foram respostas certas sem o número exigido pelo caso.
- `tools_bench` completo: casos antigos 120/135 (v8 117–119/132), casos novos 29/30. Teste de
  fumaça ok.

## Consequências

**Positivas**

- O histórico cresce bem mais devagar com ferramentas, e as notícias e a busca podem devolver mais
  texto na hora sem pesar nos turnos seguintes.
- Pergunta de acompanhamento ("e as sessões?") traz dado atual, não o de minutos atrás.

**Negativas / riscos**

- Um detalhe da consulta anterior que ela não falou some: para ele, ela consulta de novo (no clima,
  o cache devolve em 1 ms; no Zabbix e no Oracle, ~0,5–1 s).
- O histórico deixa de ser a cópia exata do que foi ao LLM: quem mexer no `_remember` precisa
  manter a troca só no fim do último turno, senão o cache inteiro se perde.
- Se o contexto voltar a apertar (memória cheia, conversas longas com a busca), a saída é a opção B.
