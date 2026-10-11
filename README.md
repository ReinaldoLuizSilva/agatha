# Agatha

Assistente virtual por voz, estilo Jarvis/Alexa, rodando **100% local** num servidor caseiro.
O foco é ter respostas rápidas, precisas e úteis no dia a dia e no trabalho com infraestrutura
(Oracle, Zabbix, GitHub).

> Status: **em construção** (Fase 0, esqueleto do repositório).

## Arquitetura

```
 PC Windows (cliente)                          agatha-srv (servidor, LAN)
┌──────────────────────────┐                  ┌───────────────────────────────────┐
│ microfone                │                  │ núcleo: orquestração, contexto,   │
│  → wake word "Agatha"    │   WebSocket      │         memória, ferramentas      │
│    (openWakeWord)        │ ───────────────► │ VAD (Silero) → STT (faster-whisper)│
│  → captura de áudio      │                  │ LLM (llama.cpp, GPU)              │
│ alto-falante             │ ◄─────────────── │ TTS (Piper)                       │
│  ← reprodução            │  áudio (stream)  │ ferramentas: Zabbix, Oracle, ...  │
└──────────────────────────┘                  └───────────────────────────────────┘
```

- **Cliente** (Windows): captura de áudio, wake word e reprodução.
- **Servidor** (Oracle Linux + GPU NVIDIA): núcleo, STT, LLM e TTS, em containers Podman.
- **Streaming em todo o pipeline**. Meta: menos de 2 s entre o fim da fala e o início da resposta.

O diferencial do projeto é o **núcleo próprio**. Os componentes de base são projetos open source
consolidados: [openWakeWord](https://github.com/dscripka/openWakeWord),
[Silero VAD](https://github.com/snakers4/silero-vad),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[llama.cpp](https://github.com/ggml-org/llama.cpp) e [Piper](https://github.com/rhasspy/piper).

As decisões de arquitetura estão registradas em [`docs/adr/`](docs/adr/).

## Estrutura do repositório

```
agatha/
├── server/    # núcleo, STT, LLM, TTS (roda no agatha-srv)
├── client/    # captura de áudio + wake word (roda no Windows)
├── tools/     # integrações: Zabbix, Oracle, GitHub…
├── deploy/    # systemd, containers (Podman)
├── bench/     # medições de latência
└── docs/adr/  # registro de decisões de arquitetura
```

## Hardware do servidor

| Item  | Especificação                                        |
|-------|------------------------------------------------------|
| SO    | Oracle Linux 10, kernel UEK 8                        |
| CPU   | Intel i5-7400 (4 núcleos, AVX2)                      |
| RAM   | 16 GB DDR4                                           |
| GPU   | NVIDIA GTX 1050 Ti, 4 GB VRAM (Pascal), driver 580   |
| Stack | Podman + NVIDIA Container Toolkit, CUDA 12.x         |

Modelos, áudio e dados de runtime ficam fora do repositório, em `/dados/agatha/{models,data,logs}`.

## Roadmap

- [x] **Fase 0**: servidor preparado e esqueleto do repositório
- [ ] **Fase 1**: servidor ouve, transcreve e imprime (PT-BR)
- [ ] **Fase 2**: conversa ponta a ponta com streaming e medição de latência
- [ ] **Fase 3**: wake word "Agatha", memória de contexto, primeiras ferramentas
- [ ] **Fase 4**: serviço systemd, logs, testes, documentação final
