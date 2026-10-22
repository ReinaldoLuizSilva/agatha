# ADR-004: LLM Gemma 4 E4B (Q4_K_M) no llama.cpp, com prompt de sistema v2

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

O ADR-001 definiu um LLM de 3–4B parâmetros, quantizado, rodando na GPU com o llama.cpp. O
ADR-002 deixou para ele **até ~3,4 GB de VRAM**, já que o STT (faster-whisper `small`) ocupa
cerca de 0,4 GB dos 4 GB da GTX 1050 Ti.

A meta é ficar abaixo de 2 s entre o fim da fala e o início da resposta. O STT já consome cerca
de 0,85 s (0,5 s de VAD + 0,35 s de transcrição). Com o TTS e a rede somando uns 0,25 s, o LLM
precisa entregar **o primeiro trecho falável em menos de ~0,8 s**. Como o pipeline divide o texto
em trechos (frase, ou pausa natural em vírgula ou dois-pontos depois de ~30 caracteres), o que
importa é o tempo até esse primeiro trecho, não a resposta inteira.

Além da latência, o modelo precisa responder bem em português do Brasil e seguir instruções de
estilo falado: respostas curtas, sem markdown, sem inventar o que não sabe.

### Medições

Feitas com `bench/llm_bench.py`, com o llama.cpp **b11205** num container próprio
(`deploy/llama/Containerfile`, CUDA 12.9.1, compilado só para `sm_61`) e as opções `-ngl 99`,
`-c 4096`, `-np 1`, `--jinja` e `--reasoning-budget 0`. Foram 13 conversas em PT-BR (dia a dia,
conta, hora, persona, Oracle, Zabbix, Linux, DevOps e um diálogo de dois turnos), com temperatura
0,6 e o STT carregado na GPU ao mesmo tempo. A VRAM medida é a da GPU inteira, incluindo os
~395 MB do STT.

Primeira rodada, com o prompt v1 (`bench/results/llm-v1-20261011-173459.*` e `-173816.*`):

| modelo (Q4_K_M)        | TTFT p50 | 1º trecho p50 / p90 | tok/s | VRAM pico |
|------------------------|----------|---------------------|-------|-----------|
| Qwen3.5-4B             | 0,22 s   | 1,11 / 1,53 s       | 19,1  | 3305 MB   |
| Qwen3-4B-Instruct-2507 | 0,12 s   | 0,82 / 1,41 s       | 21,7  | 3485 MB   |
| Gemma 4 E2B            | 0,13 s   | 0,40 / 0,68 s       | 31,4  | 2077 MB   |
| Gemma 4 E4B            | 0,25 s   | 1,16 / 1,38 s       | 18,3  | 3661 MB   |
| Gemma 3 4B             | 0,53 s   | 0,72 / 1,24 s       | 21,3  | 3153 MB   |

Na leitura das respostas, todos os modelos usaram markdown, emoji ou respostas longas em algum
momento, e três deles inventaram a hora. O **prompt v2** ataca esses pontos:

- pede uma primeira frase de até oito palavras;
- proíbe ler código ou SQL;
- proíbe markdown e símbolos;
- evita cumprimentar a cada resposta;
- deixa explícito quem é quem.

Segunda rodada, só com os três finalistas e o prompt v2 (`bench/results/llm-v2-20261011-174016.*`):

| modelo                 | TTFT p50 | 1º trecho p50 / p90 | tok/s | VRAM pico | tokens/resposta |
|------------------------|----------|---------------------|-------|-----------|-----------------|
| Qwen3-4B-Instruct-2507 | 0,12 s   | 0,79 / 1,14 s       | 21,6  | 3487 MB   | 43              |
| Gemma 4 E2B            | 0,13 s   | 0,42 / 0,54 s       | 31,3  | 2077 MB   | 27              |
| **Gemma 4 E4B**        | 0,25 s   | **0,59 / 0,70 s**   | 18,0  | 3661 MB   | 38              |

Qualidade dos finalistas, pela leitura das respostas feita pelo Claude (a escolha foi aprovada pelo Reinaldo):

| teste                                | Qwen3-2507                     | Gemma 4 E2B              | Gemma 4 E4B           |
|--------------------------------------|--------------------------------|--------------------------|-----------------------|
| 15% de 240                           | certo no v1, errado no v2 (54) | errado nas duas (66)     | **certo nas duas**    |
| "que horas são?"                     | inventou no v1, honesto no v2  | inventou e se contradisse | **honesto nas duas** |
| persona (quem é quem)                | certo                          | confuso                  | certo no v2           |
| conceitos (tablespace, trigger, Podman) | corretos                    | rasos                    | corretos              |
| curiosidade científica               | inventou um fato               | vaga                     | correta               |
| estilo falado                        | ainda usa crase e quebra de linha | bom                   | bom                   |

**Nenhum modelo acertou como consultar o tamanho das tablespaces no Oracle.** Todos citaram views
ou comandos errados. Isso é limitação de modelo de 4B, não de um candidato específico.

## Decisão

1. **Modelo:** Gemma 4 E4B instruct, quantização Q4_K_M.
   - Arquivo: `gemma-4-E4B-it-Q4_K_M.gguf` (repositório `unsloth/gemma-4-E4B-it-GGUF`, 4 977 171 584 bytes).
   - SHA-256: `85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87`.
   - Licença: Apache 2.0.
   - Local: `/dados/agatha/models/llm/`, fora do git.

2. **Servidor:** imagem `localhost/agatha-llama:b11205` (`deploy/llama/Containerfile`), com Podman
   sem root e a GPU via CDI. Opções:
   - `-ngl 99`: todas as camadas na GPU;
   - `-c 4096`;
   - `-np 1`: um slot. O padrão automático multiplica o KV cache;
   - `--jinja`;
   - `--reasoning-budget 0`: sem modo de raciocínio;
   - flash attention em `auto`, que é o padrão.

   A API é a compatível com OpenAI (`/v1/chat/completions`) em streaming, com `cache_prompt` para
   reaproveitar o prompt de sistema.

3. **Amostragem:** temperatura 0,6.

4. **Prompt de sistema v2**, como ponto de partida. Ele vai evoluir com o uso e ficará no núcleo
   do servidor. A versão medida está em `bench/llm_bench.py` (`SYSTEM_PROMPTS["v2"]`). O núcleo
   também vai:
   - **injetar a data e a hora atuais** no contexto de cada turno;
   - **limpar o texto antes do TTS** (markdown, emoji, símbolos), sem depender só do prompt.

5. **Orçamento de VRAM atualizado** (medido, GPU inteira):

   | componente                  | VRAM      |
   |-----------------------------|-----------|
   | STT (`small`, int8)         | ~0,40 GB  |
   | LLM (Gemma 4 E4B, ctx 4096) | ~3,27 GB  |
   | **pico total**              | **3,66 GB** de 4,0 GB |

   Parte dos pesos do E4B são embeddings por camada que o llama.cpp mantém na RAM. Por isso o
   arquivo tem 5 GB e a VRAM usada é bem menor.

## Consequências

**Positivas**

- **Latência estimada de ~1,7 s (p50) e ~1,85 s (p90)** entre o fim da fala e o primeiro áudio:
  0,5 s de VAD + 0,35 s de STT + 0,59 s do 1º trecho + ~0,15 s de TTS + ~0,1 s de rede. Fica
  abaixo da meta de 2 s, a confirmar na medição de ponta a ponta.
- O mais consistente dos finalistas em conta, em admitir o que não sabe e em estilo falado.
- Apache 2.0, sem aceite de termos para baixar.
- Trocar de modelo exige só mudar o arquivo GGUF e as opções, porque o benchmark é reproduzível.

**Negativas / riscos**

- **VRAM apertada:** sobram ~0,43 GB na GPU, e o E4B fica perto do teto de 3,4 GB do ADR-002.
  Contextos maiores que 4096 tokens exigirão quantizar o KV cache (`--cache-type-k/v q8_0`) ou
  limitar o histórico. Qualquer componente novo na GPU precisa ser medido antes.
  *Revisão de 22/10 (ADR-023):* medido, o contexto de 6144 custou só +34 MiB (sliding window
  attention), sem quantizar o KV cache; adotado.
- **Geração mais lenta que os outros** (18 tok/s): respostas longas demoram mais para terminar,
  embora comecem rápido. O prompt de respostas curtas é parte da decisão.
- **Conhecimento técnico limitado:** um modelo de 4B erra detalhes de Oracle e Zabbix e não é
  confiável em contas. Isso se resolve com **ferramentas** (Fase 3: consultas reais ao Oracle e ao
  Zabbix, calculadora), não com um modelo maior, que não caberia na GPU.
- **Avaliação limitada:** foram 13 conversas, julgadas por leitura. A escolha deve ser revista com
  o uso real.

**Alternativas, se for preciso mudar**

- **Gemma 4 E2B:** se a VRAM ou a latência apertarem. Responde mais rápido (0,42 s) e usa 1,6 GB a
  menos, mas erra mais.
- **Qwen3-4B-Instruct-2507:** se o conhecimento técnico pesar mais que o estilo. Tem conhecimento
  parecido, oscila mais e chega perto do limite de 2 s.
