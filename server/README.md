# server/

Servidor da Agatha, que roda no `agatha-srv`. Na Fase 1 ele recebe áudio pelo WebSocket, detecta
as frases com o Silero VAD, transcreve com o faster-whisper na GPU e devolve o texto com os tempos
de cada etapa.

- Protocolo: [`docs/protocol.md`](../docs/protocol.md) ([ADR-003](../docs/adr/0003-protocolo-websocket.md))
- STT: faster-whisper `small`, int8, GPU ([ADR-002](../docs/adr/0002-stt-faster-whisper-small-gpu.md))

## Estrutura

```
agatha_server/
├── __main__.py   # python -m agatha_server
├── config.py     # variáveis AGATHA_* e .env
├── protocol.py   # mensagens e validação do protocolo v1
├── vad.py        # Silero VAD em streaming + segmentação em frases
├── stt.py        # faster-whisper (carrega cuBLAS/cuDNN do venv)
└── app.py        # servidor WebSocket
scripts/
└── send_wav.py   # cliente de teste: envia WAVs em tempo real
tests/
```

## Instalação (desenvolvimento)

Os comandos partem da raiz do repositório:

```bash
python3 -m venv .venv
.venv/bin/pip install -e "server[gpu,dev]"
```

O extra `gpu` instala o cuBLAS 12 e o cuDNN 9 via pip, dentro do venv. O host fica só com o
driver NVIDIA (branch 580).

## Configuração

```bash
cd server
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # cole em AGATHA_TOKEN
chmod 600 .env
```

Todas as opções (porta, modelo, parâmetros do VAD) estão comentadas no `.env.example`. As
variáveis de ambiente têm prioridade sobre o `.env`.

## Execução

```bash
cd server
../.venv/bin/python -m agatha_server                        # escuta em 0.0.0.0:8765
AGATHA_HOST=127.0.0.1 ../.venv/bin/python -m agatha_server  # só local
```

O modelo é lido de `/dados/agatha/models/whisper`. Se não estiver lá, é baixado na primeira partida.

## Testes

```bash
cd server
../.venv/bin/python -m pytest
```

Os testes do protocolo usam VAD e STT falsos. Os do Silero e do STT usam o modelo real e o FLEURS
(`/dados/agatha/data/fleurs`). O STT exige GPU, e os testes que dependem de algo ausente são pulados.

## Teste de ponta a ponta sem microfone

Com o servidor rodando:

```bash
cd server
../.venv/bin/python scripts/send_wav.py /dados/agatha/data/fleurs/pt_br/dev/7450437965739451777.wav
../.venv/bin/python scripts/send_wav.py --out ../bench/results/e2e-stt-local-$(date +%Y%m%d-%H%M%S).json arquivos*.wav
```

Os arquivos precisam estar em 16 kHz. O script envia blocos de 32 ms no ritmo real e mostra os
eventos `vad`, as transcrições e um resumo das latências.
