# ADR-018: Precisão — benchmark das falas reais, prompt v5 e STT no limite do modelo

- **Status:** Aceito
- **Data:** 2026-10-18
- **Complementa:** [ADR-002](0002-stt-faster-whisper-small-gpu.md) (STT), [ADR-004](0004-llm-gemma-4-e4b.md) (LLM),
  [ADR-009](0009-ferramentas-tool-calling.md) (ferramentas)

## Contexto

Depois da Fase 5, a velocidade estava boa (1,7–2,6 s entre o fim da fala e o 1º som), e o ponto
fraco era a precisão. Nas 52 falas reais do registro de turnos (Fases 4 e 5), quase todo erro
começava no STT e o LLM piorava o que chegava errado:

- **o STT trocava jargão:** "Rima"/"RMA" (RMAN), "abics" (Zabbix), "incompleto" no lugar de
  diferencial, "valer" no lugar de validar;
- **o LLM inventava em cima:** explicou um "backup incompleto", disse que "RMA = devolução de
  mercadoria" e que "SPDP = Storage Pluggable Database";
- **o LLM recusava o que sabia:** "não tenho acesso a manuais técnicos", e "não tenho
  informações financeiras" sobre CDB e PDB;
- **o LLM negava as ferramentas:** "não tenho acesso a sistemas", tendo o Zabbix;
- **o LLM não usava a ferramenta quando o host era óbvio:** "como está o servidor?" → "qual
  servidor?", sendo que o laboratório só monitora um.

Restrições: a VRAM não comporta modelo maior (sobram ~0,4 GB, ADR-004), sem serviço pago, e a
latência não pode piorar.

## Decisão

1. **Medir antes de corrigir, com dados dele.**
   - [`bench/precision_bench.py`](../../bench/precision_bench.py): as perguntas reais **como o STT
     as entregou**, cada uma rotulada com o comportamento esperado (responder; confirmar o termo
     estragado; usar a ferramenta; dizer que não consegue saber). Usa o mesmo prompt e as mesmas
     ferramentas do servidor. São 33 casos × 3 rodadas.
   - [`bench/stt_jargon_bench.py`](../../bench/stt_jargon_bench.py): 40 frases de jargão gravadas
     pelo Reinaldo (`python -m agatha_client.record --part jargao`). Usa o pipeline do servidor no
     CPU, fora da Agatha, para não disputar a GPU nem tocar na memória real. Mede o WER e o
     acerto de cada termo de jargão. As 70 frases da Fase 5 servem de controle de vazamento.
2. **Prompt v5** (`server/agatha_server/prompts.py`):
   - conceito de TI se responde direto; "não sei" só vale para o estado de algo que nenhuma
     ferramenta mostra (backup, locks), e ela nunca inventa um estado;
   - diante de uma palavra estranha, ela pensa no termo técnico de som parecido e confirma, e
     nunca explica uma palavra que não reconhece;
   - um glossário curto das siglas do Reinaldo (Oracle, Zabbix, tipos de backup);
   - as ferramentas dão acesso real; só se chama uma se ela mostra o que foi perguntado, e o que
     o resultado não traz não vira conclusão.
3. **Host padrão do Zabbix** (`AGATHA_ZABBIX_DEFAULT_HOST=agatha-srv`): "o servidor", sem nome, é
   esse host. A regra da ferramenta só cita o host se ele estiver configurado.
4. **STT sem mudança.** O vocabulário em lista, que já estava em produção, é a melhor opção
   barata medida (tabela abaixo). Os erros que sobram são acústicos ("LISR" por listener, "Linux
   bom" por Ansible, "Puskas" por PostgreSQL), não falta de vocabulário: o Whisper `small` está no
   limite com essa voz e esse microfone. O prompt v5 compensa do lado do LLM, pedindo
   confirmação.
5. **Corte de fim de fala continua em 500 ms.** Em 74 falas reais, só 2 foram cortadas por pausa,
   e as duas foram juntadas pela continuação (`continuation_ms`). Subir para 700 ms custaria
   +200 ms em todas as falas.

### Resultados

LLM (`precision_bench`, 3 rodadas; as duas correções de critério, como acento e plural, foram
aplicadas a todas as versões com `--rescore`):

| prompt | responder | confirmar | ferramenta | não consigo | total | invenções conhecidas |
|---|---|---|---|---|---|---|
| v3 (anterior) | 20/33 | 9/21 | 12/21 | 12/15 | **53/90** | 6 |
| v4 | 28/33 | 15/21 | 15/21 | 12/15 | 70/90 | 5 |
| **v5** | 30/33 | 18/21 | 15/21 | 12/15 | **75/90** | **0** |

Com 3 casos novos de siglas conhecidas (STT, LLM, HTTP), a v5 fez 81/99. As tentativas de
corrigir o "você quis dizer" em excesso ficaram abaixo, dentro do ruído: v6 (explicar direto as
siglas conhecidas) 76/99 e v7 (sem o exemplo concreto, que o modelo copiou uma vez: "STT?" →
"Você quis dizer Data Guard?") 78/99. O `tools_bench` da Fase 3 ficou em 94/105 (era 92/105):
nada regrediu. O prefixo do prompt foi de 1784 para 1983 tokens (+11%, ~0,7 s a mais no
pré-aquecimento).

STT (40 frases de jargão, 97 ocorrências de termos):

| variante | jargão certo | WER |
|---|---|---|
| sem vocabulário | 62 (64%) | 35,7% |
| **vocabulário em lista (produção)** | **79 (81%)** | 25,9% |
| vocabulário escrito como frase | 72 (74%) | 26,2% |
| `hotwords` | 79 (81%) | 24,9% |
| `beam_size` 5 | 80 (82%) | 23,0% (+13% de tempo; risco de VRAM) |
| lista ampliada (+14 termos) | 77 (79%) | 24,3% (sem vazamento no controle) |
| volume normalizado (pico −3 dBFS) | 79 (81%) | 25,2% |

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Whisper `medium`/`large-v3-turbo` | não cabe na VRAM com o LLM; no CPU (i5-7400) passaria da meta de latência |
| Corrigir as trocas conhecidas do STT com um dicionário ("Puskas" → PostgreSQL) | frágil e só pega o que já foi visto; a confirmação no LLM cobre o caso geral |
| Glossário no vocabulário do STT | medido: a lista ampliada não ganhou nada |
| Corte de fim de fala em 700 ms | +200 ms em todas as falas para resolver ~3% delas |

## Consequências

**Positivas**

- Nas perguntas reais: 53/90 → 75/90, e as invenções conhecidas foram a zero. Ela responde o que
  sabe, confirma o termo estranho e usa o Zabbix para "o servidor".
- Os dois benchmarks ficam no repositório para medir as próximas mudanças, como uma voz, um
  modelo ou uma ferramenta nova.

**Negativas / riscos**

- **O benchmark do LLM foi escrito a partir dos próprios erros que se queria corrigir.** Para não
  inflar o resultado, os exemplos do prompt não repetem os casos do benchmark, e os critérios só
  mudaram para corrigir falsos negativos, aplicados a todas as versões. A prova de verdade é o uso:
  vale acompanhar o registro de turnos.
- **O "você quis dizer…?" às vezes aparece com siglas conhecidas** ("Você quis dizer STT, como
  Speech-to-Text?"). As duas tentativas de corrigir pioraram o resto, então ficou como está.
- **O STT continua errando ~1 em cada 5 termos de jargão** com a voz baixa dele. Melhorar isso
  depende de hardware (GPU com mais VRAM) ou de outro microfone.
- O prompt maior deixa ~14 turnos de histórico com a memória cheia, contra ~16 antes.
