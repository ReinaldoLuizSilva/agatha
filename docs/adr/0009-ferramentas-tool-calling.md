# ADR-009: Ferramentas via tool calling do LLM, com exemplos no prompt

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

A Fase 3 dá ferramentas à Agatha: calculadora, memória, consultas ao Zabbix e ao Oracle. Nas
fases anteriores ficou claro que um modelo de 4B **erra contas** e **inventa dados** que não tem
(ADR-004, ADR-007). A forma natural de resolver isso é a chamada de ferramentas (*tool calling*)
da API compatível com OpenAI. O template do Gemma 4 no llama-server suporta, inclusive em
streaming.

O `bench/tools_bench.py` mediu o Gemma 4 E4B com 22 perguntas em PT-BR: contas, Zabbix, Oracle,
"lembra que…" e 8 perguntas que **não** pedem ferramenta. Os resultados estão em
`bench/results/tools-*.json`.

| variante do prompt | escolha certa (com ferramenta) | escolha certa (sem) | argumentos certos |
|---|---|---|---|
| instrução simples (v1) | ~50% (rodada interrompida por erro 500) | 100% | — |
| regras explícitas (v2) | 14/28 | 16/16 | 14/28 |
| v2 + **exemplos de conversa** | 56/56 (4 rodadas) | 32/32 | 52/56 |
| **regras v3 + exemplos**, inclusive de conceitos | **42/42** (3 rodadas) | **24/24** | 40/42 |

Sem exemplos, o modelo:
- **narrava a ferramenta** em vez de chamá-la ("Vou usar a ferramenta de tablespaces…");
- respondia **de cabeça** ("Deixa eu calcular isso. O resultado é 36.");
- dizia "vou lembrar" sem gravar nada;
- mudava de comportamento entre rodadas.

Numa chamada, gerou um **argumento degenerado** (um número com milhares de dígitos), e o
llama-server devolveu HTTP 500. Com os exemplos de ferramentas, perguntas conceituais sobre o
Zabbix passaram a receber "não tenho informações". Um exemplo de conceito respondido direto
resolveu isso.

## Decisão

1. **Ferramentas pelo tool calling nativo do llama-server**, em streaming, sem chamadas paralelas
   (`parallel_tool_calls: false`) e com **no máximo 2 rodadas** de ferramenta por resposta. A
   última ida ao LLM vai sem ferramentas.

2. **Cada ferramenta (`server/agatha_server/tools/`) declara:**
   - o esquema;
   - a **regra** que entra no prompt de sistema ("qualquer conta…: use calcular, mesmo que pareça
     fácil");
   - os **seus próprios exemplos de uso**.

   O prompt e os exemplos são montados a partir das ferramentas registradas, então um exemplo
   nunca menciona uma ferramenta que não existe.

3. **Exemplos fixos no começo da conversa** (few-shot):
   - uma troca completa por ferramenta (pergunta → chamada → resultado → resposta curta);
   - dois exemplos de **conceito respondido sem ferramenta** (um deles sobre o Zabbix).

   Esses exemplos, as regras e a lista de ferramentas fazem parte do **prefixo fixo** do prompt.
   Por isso entram no cache do llama.cpp e no pré-aquecimento (ADR-007), e não custam latência
   por turno.

4. **Frase de espera:** assim que chega o primeiro pedaço de uma chamada de ferramenta (~0,6 s
   depois do início da geração), a Agatha fala "Deixa eu ver.", "Um instante." ou "Só um
   momento.", alternando entre elas. Depois executa a ferramenta e volta ao LLM com o resultado.

5. **Robustez:**
   - erro da ferramenta (argumento inválido, exceção, timeout de 10 s) vira `{"erro": ...}` para
     o LLM explicar;
   - falha do llama-server com ferramentas (o HTTP 500 do JSON degenerado) faz a Agatha tentar de
     novo **sem ferramentas**.

6. **Histórico:** o turno inteiro (pergunta, chamada, resultado e resposta) é guardado exatamente
   como foi enviado, para preservar o cache do prompt (ADR-007).

7. **Primeira ferramenta: calculadora segura.** Ela avalia só aritmética pela árvore sintática do
   Python, **sem `eval`**, com limite de expoente. Ferramentas que leem sistemas (Zabbix, Oracle)
   serão **somente leitura** nesta fase.

8. **Carimbo de data repetido:** com ferramentas no prompt, o modelo às vezes repete o carimbo de
   data da mensagem ("[domingo, 11/10/2026, 21h30] …"). Um filtro remove o carimbo do começo da
   resposta antes da divisão em trechos.

## Consequências

**Positivas**

- Contas exatas e, nas próximas etapas, dados reais em vez de inventados.
- Medido ao vivo: "quanto é 15% de 240?" → frase de espera + `calcular(240 * 0.15)` → "É 36.",
  com o **primeiro som em ~1,5–1,6 s**, mais cedo que numa resposta comum. Uma continuação como "e
  se fosse 20%?" usa o contexto da conversa.
- Novas ferramentas entram declarando esquema, regra e exemplo, sem mexer no núcleo.

**Negativas / riscos**

- **Uma ida extra ao LLM** por ferramenta. A frase de espera disfarça, mas a resposta completa
  demora mais (~0,6 s para a 2ª ida começar a falar, além do tempo da ferramenta).
- **O prompt fica maior** a cada ferramenta e exemplo: o cache cobre os turnos, mas a primeira
  pergunta depois de subir o LLM e o pré-aquecimento depois de um corte do histórico ficam mais
  caros (~2,9 s para aquecer o prefixo com a calculadora). Também consome contexto (4096 tokens).
- **O raciocínio continua sendo do modelo:** a calculadora garante a aritmética, não a equação.
  No problema "1,5 TB, cresce 20 GB por dia, quando chega a 2 TB?", o modelo montou a conta
  misturando tera e giga.
- **A confiabilidade depende dos exemplos:** foram medidas 66 chamadas com 6 ferramentas. Cada
  ferramenta nova precisa de um exemplo e deve ser medida no benchmark antes de ir para o uso.
