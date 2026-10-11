# bench/

Medições de latência da Agatha. A meta do projeto é ficar abaixo de 2 s entre o fim da fala e o
início da resposta. Cada etapa do pipeline é medida aqui antes de qualquer escolha.

Os resultados brutos ficam em `results/` (JSON, versionados) para comparar rodadas ao longo do tempo.

## stt_bench.py: STT (faster-whisper) na CPU

Compara tamanhos de modelo Whisper em PT-BR e mede:

- **WER**: taxa de erro de palavras contra a transcrição de referência, com texto normalizado
  (minúsculas, sem pontuação, números por extenso);
- **latência e RTF** em frases completas (5–30 s). O RTF é o tempo de processamento ÷ a duração do áudio;
- **latência em comandos curtos**: recortes de 3 s, o caso típico de um comando de voz. O encoder
  do Whisper sempre processa uma janela de 30 s, então frases curtas não ficam proporcionalmente
  mais rápidas;
- tempo de carga e pico de RAM, com cada modelo rodando num processo separado.

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

### Execução

```bash
.venv/bin/python bench/stt_bench.py                       # todos os modelos, 30 frases
.venv/bin/python bench/stt_bench.py --models small --n 10 # rodada rápida
.venv/bin/python bench/stt_bench.py --help
```

### Limitações

- O FLEURS é fala **lida** de frases da Wikipédia, não comandos de assistente. O WER serve para
  comparar os modelos entre si, não como medida absoluta do uso real. A validação com gravações
  do próprio usuário (jargão de Zabbix, Oracle etc.) fica para depois.
- A latência dos comandos curtos é medida em recortes de 3 s das mesmas frases, que podem cortar
  uma palavra no meio. Só o tempo vale aqui, não o texto.
