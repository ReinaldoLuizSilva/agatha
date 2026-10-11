# bench/

Medições de latência da Agatha. A meta do projeto é ficar abaixo de 2 s entre o fim da fala e o
início da resposta. Cada etapa do pipeline é medida aqui antes de qualquer escolha.

Os resultados brutos ficam em `results/` (JSON, versionados) para comparar rodadas ao longo do tempo.

## stt_bench.py: STT (faster-whisper) na CPU ou na GPU

Compara tamanhos de modelo Whisper em PT-BR e mede:

- **WER**: taxa de erro de palavras contra a transcrição de referência, com texto normalizado
  (minúsculas, sem pontuação, números por extenso);
- **latência e RTF** em frases completas (5–30 s). O RTF é o tempo de processamento ÷ a duração do áudio;
- **latência em comandos curtos**: recortes de 3 s, o caso típico de um comando de voz. O encoder
  do Whisper sempre processa uma janela de 30 s, então frases curtas não ficam proporcionalmente
  mais rápidas;
- tempo de carga, pico de RAM e, na GPU, VRAM usada, com cada modelo rodando num processo separado.

### Preparação (uma vez)

```bash
python3 -m venv .venv
.venv/bin/pip install -r bench/requirements.txt

# FLEURS pt_br (Google, CC-BY-4.0), split dev: ~250 MB
D=/dados/agatha/data/fleurs/pt_br
mkdir -p $D && cd $D
curl -LO https://huggingface.co/datasets/google/fleurs/resolve/main/data/pt_br/dev.tsv
curl -LO https://huggingface.co/datasets/google/fleurs/resolve/main/data/pt_br/audio/dev.tar.gz
tar -xzf dev.tar.gz && rm dev.tar.gz
```

Os modelos são baixados sob demanda para `/dados/agatha/models/whisper/`.

Para rodar na GPU, instale também as bibliotecas CUDA 12 (cuBLAS e cuDNN 9) que a NVIDIA publica
no PyPI. Elas ficam só no venv: o host continua apenas com o driver (branch 580). O script ajusta
o `LD_LIBRARY_PATH` sozinho.

```bash
.venv/bin/pip install -r bench/requirements-gpu.txt
```

### Execução

```bash
.venv/bin/python bench/stt_bench.py                       # todos os modelos, 30 frases
.venv/bin/python bench/stt_bench.py --models small --n 10 # rodada rápida
.venv/bin/python bench/stt_bench.py --device cuda         # na GPU
.venv/bin/python bench/stt_bench.py --help
```

### Limitações

- O FLEURS é fala **lida** de frases da Wikipédia, não comandos de assistente. O WER serve para
  comparar os modelos entre si, não como medida absoluta do uso real. A validação com gravações
  do próprio usuário (jargão de Zabbix, Oracle etc.) fica para depois.
- A latência dos comandos curtos é medida em recortes de 3 s das mesmas frases, que podem cortar
  uma palavra no meio. Só o tempo vale aqui, não o texto.

## llm_bench.py: LLMs no llama.cpp (GPU, em container)

Sobe cada modelo GGUF num container do `llama-server` (`deploy/llama/Containerfile`) e mede, com
o prompt de sistema da Agatha e 13 conversas em PT-BR: tempo até o primeiro token, até o
primeiro **trecho falável** (frase, ou pausa natural numa frase longa), tokens/s e pico de VRAM da
GPU inteira. Gera um JSON e um Markdown com as respostas lado a lado, para avaliação humana.
Resultado e escolha: [ADR-004](../docs/adr/0004-llm-gemma-4-e4b.md).

```bash
deploy/down.sh                  # libera a GPU e a porta 8080
.venv/bin/python bench/llm_bench.py --system v2 --models gemma-4-e4b,qwen3-4b-2507
```

Os modelos ficam em `/dados/agatha/models/llm/`.

## tts_bench.py: vozes PT-BR na CPU

Compara vozes do Piper e do Kokoro em frases típicas da Agatha: tempo até o primeiro áudio, RTF e
amostras WAV para ouvir (`results/tts-samples/`, fora do git). Roda num venv próprio, para as
dependências não se misturarem com as do servidor. Resultado e escolha:
[ADR-005](../docs/adr/0005-tts-piper-dii.md).

```bash
python3 -m venv .venv-tts && .venv-tts/bin/pip install -r bench/requirements-tts.txt
.venv-tts/bin/python bench/tts_bench.py
```

## Ponta a ponta: conversa simulada

O `server/scripts/send_wav.py` conversa com o servidor no ar usando arquivos WAV no lugar do
microfone, respeitando o half-duplex, e mede **fim da fala → primeiro áudio** no próprio relógio,
como o cliente real (sem a latência da placa de som, ~90 ms no PC do Reinaldo). Os resultados
`e2e-conversa-*.json` registram a evolução descrita no
[ADR-007](../docs/adr/0007-ajustes-latencia-conversa.md):

| resultado | situação | p50 | p90 |
|---|---|---|---|
| `e2e-conversa-local-20261011-181538` | primeira versão completa | 2,58 s | 3,59 s |
| `e2e-conversa-local-20261011-182730` | cache, threads, prompt v3, vocabulário | 1,94 s | 2,17 s |
| `e2e-conversa-longa-pod-20261011-190523` | 14 perguntas, em container, pela LAN | 1,84 s | 2,09 s |
