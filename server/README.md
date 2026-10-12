# server/

Servidor da Agatha, que roda no `agatha-srv`. Ele recebe o microfone pelo WebSocket, detecta as
frases (Silero VAD), transcreve (faster-whisper na GPU), gera a resposta (Gemma 4 E4B no
llama.cpp, em outro container) e a devolve falada (Piper na CPU), em streaming e em half-duplex.

| peça | escolha | decisão |
|---|---|---|
| protocolo | WebSocket, v2 (respostas faladas, half-duplex) | [ADR-003](../docs/adr/0003-protocolo-websocket.md), [ADR-006](../docs/adr/0006-protocolo-v2-half-duplex.md), [`docs/protocol.md`](../docs/protocol.md) |
| STT | faster-whisper `small`, int8, GPU | [ADR-002](../docs/adr/0002-stt-faster-whisper-small-gpu.md) |
| LLM | Gemma 4 E4B Q4_K_M no llama-server | [ADR-004](../docs/adr/0004-llm-gemma-4-e4b.md) |
| TTS | Piper, voz feminina "dii", CPU | [ADR-005](../docs/adr/0005-tts-piper-dii.md) |
| ajustes de latência | cache do prompt, threads, prompt v3, fim da fala | [ADR-007](../docs/adr/0007-ajustes-latencia-conversa.md) |
| wake word | "Agatha" na transcrição + modo conversa | [ADR-008](../docs/adr/0008-wake-word-pela-transcricao.md) |
| ferramentas | tool calling com exemplos no prompt; calculadora | [ADR-009](../docs/adr/0009-ferramentas-tool-calling.md) |
| memória | fatos guardados por pedido, SQLite | [ADR-010](../docs/adr/0010-memoria-explicita.md) |

## Como uma resposta acontece

```
microfone ─► VAD (frases) ─► STT ─► transcrição ──► parece cortada? ─sim─► espera a continuação
                                                        │não
                                                        ▼
               Conversation: prompt v3 + histórico + [data e hora] ─► LLM (streaming)
                                                        │
                       Chunker (frase, ou vírgula cedo) ─► clean ─► normalize_for_speech ─► Piper
                                                        │
                                              response_text + áudio ─► cliente
```

Enquanto responde, a sessão ignora o microfone até o cliente avisar `playback_end`.

## Estrutura

```
agatha_server/
├── __main__.py      # python -m agatha_server: carrega STT e TTS, sobe o WebSocket
├── config.py        # variáveis AGATHA_* e .env
├── protocol.py      # mensagens e validação do protocolo v2
├── app.py           # servidor e sessões (ouvindo/respondendo, frase cortada, prazos)
├── vad.py           # Silero VAD em streaming + segmentação em frases
├── stt.py           # faster-whisper (carrega cuBLAS/cuDNN do pip)
├── conversation.py  # orquestra uma resposta: LLM → trechos → TTS, histórico, cache
├── llm.py           # cliente do llama-server (streaming, pré-aquecimento do cache)
├── prompts.py       # prompt de sistema, regras das ferramentas, exemplos, memória, data/hora
├── text.py          # divisão em trechos, limpeza, normalização para a fala, frase incompleta
├── tts.py           # Piper com threads ajustadas
├── wake.py          # wake word pela transcrição
├── memory.py        # memória explícita (SQLite) + CLI de administração
└── tools/           # ferramentas do LLM: base (registro), calc, memory
scripts/
└── send_wav.py      # cliente simulado: conversa usando WAVs, mede a latência de ponta a ponta
tests/
```

## Rodar em produção (pod Podman)

O jeito normal de rodar é o pod `agatha`, com o LLM e este servidor em containers. Veja
[`deploy/README.md`](../deploy/README.md):

```bash
deploy/up.sh                    # sobe tudo
podman logs -f agatha-server    # acompanha
deploy/down.sh                  # derruba
```

## Desenvolvimento (venv)

Os comandos partem da raiz do repositório:

```bash
python3 -m venv .venv
.venv/bin/pip install -e "server[gpu,dev]"
```

O extra `gpu` instala o cuBLAS 12 e o cuDNN 9 via pip, dentro do venv. O host fica só com o
driver NVIDIA (branch 580).

Para rodar o servidor pelo venv, o LLM precisa estar no ar (pelo pod, com `deploy/up.sh`, e
depois `podman stop agatha-server` para liberar a porta 8765 e a VRAM do STT):

```bash
cd server
../.venv/bin/python -m agatha_server
```

## Memória

Os fatos que a Agatha guarda ficam em `/dados/agatha/data/memory/agatha.db`. Para ver ou editar:

```bash
podman exec agatha-server python3.12 -m agatha_server.memory list
podman exec agatha-server python3.12 -m agatha_server.memory forget 3
```

## Configuração

```bash
cd server
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # cole em AGATHA_TOKEN
chmod 600 .env
```

Todas as opções estão comentadas no `.env.example`: porta, STT, VAD (inclusive
`AGATHA_VAD_CONTINUATION_MS`), LLM, histórico e TTS. As variáveis de ambiente têm prioridade
sobre o `.env`. No container, os caminhos dos modelos já vêm definidos na imagem.

## Testes

```bash
cd server
../.venv/bin/python -m pytest
```

Os testes do protocolo, das sessões e da conversa usam VAD, STT, LLM e TTS falsos. Os do Silero e
do STT usam os modelos reais e o FLEURS (`/dados/agatha/data/fleurs`). Os de STT precisam de
~0,7 GB de VRAM livre e são **pulados** quando o pod está no ar, porque a GPU fica quase cheia.

## Conversa de teste sem microfone

Com o servidor no ar, o `send_wav.py` conversa usando arquivos WAV de 16 kHz. Cada arquivo é uma
fala. Ele simula a reprodução, envia o `playback_end` e mede o tempo entre o fim da fala e o
primeiro áudio no próprio relógio:

```bash
cd server
../.venv/bin/python scripts/send_wav.py --url ws://192.168.1.25:8765/ pergunta1.wav pergunta2.wav
../.venv/bin/python scripts/send_wav.py --save-replies /tmp/respostas \
    --out ../bench/results/e2e-conversa-$(date +%Y%m%d-%H%M%S).json perguntas/*.wav
```
