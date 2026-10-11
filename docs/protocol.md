# Protocolo cliente ↔ servidor

Especificação do protocolo WebSocket entre o cliente (PC Windows) e o servidor (`agatha-srv`).
As decisões e os motivos estão no [ADR-003](adr/0003-protocolo-websocket.md).

- **Versão atual:** 1 (Fase 1: o cliente envia áudio, o servidor devolve transcrições)
- **Endereço:** `ws://<servidor>:8765/`

## Visão geral

Uma única conexão WebSocket por cliente, com dois tipos de frame:

- **Binário:** áudio PCM, só no sentido cliente → servidor.
- **Texto:** mensagens de controle em JSON, nos dois sentidos, sempre com o campo `type`.

```
cliente                                  servidor
   │── {"type":"hello", ...} ──────────────►│
   │◄────────────── {"type":"ready", ...} ──│
   │── [PCM binário] ──────────────────────►│
   │── [PCM binário] ──────────────────────►│
   │◄─────── {"type":"vad","event":"start"} │
   │── [PCM binário] ... ──────────────────►│
   │◄───────── {"type":"vad","event":"end"} │
   │◄─────────── {"type":"transcript", ...} │
   │── [PCM binário] ... ──────────────────►│
```

## Handshake

A primeira mensagem do cliente **precisa** ser um `hello`, enviado em até 5 s após a conexão.
Antes do `ready`, o servidor rejeita qualquer áudio.

### `hello` (cliente → servidor)

```json
{
  "type": "hello",
  "protocol": 1,
  "client": "agatha-client-windows/0.1",
  "token": "<AGATHA_TOKEN>",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
}
```

| campo      | obrigatório | descrição                                                      |
|------------|-------------|----------------------------------------------------------------|
| `protocol` | sim         | versão do protocolo que o cliente fala. Hoje só `1` é aceita   |
| `client`   | não         | identificação livre do cliente, usada só em log                |
| `token`    | sim         | segredo compartilhado (`AGATHA_TOKEN` no `.env` do servidor)   |
| `audio`    | sim         | formato do áudio. Hoje só `pcm_s16le`, `16000`, `1` é aceito   |

### `ready` (servidor → cliente)

```json
{
  "type": "ready",
  "protocol": 1,
  "session": "3f9c2a71",
  "vad": {
    "threshold": 0.5,
    "min_silence_ms": 500,
    "pre_roll_ms": 300,
    "min_speech_ms": 250,
    "max_segment_s": 25
  }
}
```

`vad` informa os parâmetros em uso, só para referência e log do cliente.

## Áudio (cliente → servidor)

- Frames **binários** de PCM com sinal, 16 bits, little-endian (`s16le`), 16 kHz, mono.
- Tamanho recomendado: **512 amostras** (32 ms, 1024 bytes), o bloco nativo do VAD. O servidor
  aceita qualquer tamanho e reagrupa internamente, mas o número de bytes precisa ser par.
- O cliente envia no ritmo em que captura, sem acumular. O servidor faz a detecção de fala.

## Eventos do servidor

### `vad`

```json
{"type": "vad", "event": "start", "segment": 1, "audio_ms": 1216}
{"type": "vad", "event": "end", "segment": 1, "audio_ms": 3840, "reason": "silence"}
```

| campo      | descrição                                                                          |
|------------|------------------------------------------------------------------------------------|
| `event`    | `start`: fala confirmada. `end`: fim da frase                                       |
| `segment`  | número sequencial da frase na sessão, começando em 1                                |
| `audio_ms` | posição no fluxo de áudio da sessão. No `start` é o início real da fala; no `end` é onde o fim foi detectado |
| `reason`   | só no `end`: `silence` (silêncio de `min_silence_ms`) ou `max_length` (`max_segment_s` atingido) |

O `start` só é emitido depois de `min_speech_ms` de fala, o que filtra estalos e ruídos curtos.
Um trecho que não chega a esse mínimo é descartado sem nenhum evento.

### `transcript`

```json
{
  "type": "transcript",
  "segment": 1,
  "text": "Qual o tamanho da tablespace USERS?",
  "timings": {
    "speech_ms": 2100,
    "endpoint_ms": 512,
    "queue_ms": 0,
    "stt_ms": 341,
    "total_ms": 853
  }
}
```

| campo         | descrição                                                                  |
|---------------|----------------------------------------------------------------------------|
| `speech_ms`   | duração do segmento de fala (sem o pre-roll)                               |
| `endpoint_ms` | tempo real entre a chegada do último bloco com fala e a detecção do fim    |
| `queue_ms`    | espera na fila de transcrição (a GPU atende uma frase por vez)             |
| `stt_ms`      | tempo de transcrição                                                       |
| `total_ms`    | do último bloco com fala até o texto pronto: a parte do STT na meta de 2 s |

Um segmento que o Whisper transcreve como texto vazio gera `transcript` com `"text": ""`.

### `error`

```json
{"type": "error", "code": "unauthorized", "message": "token inválido"}
```

| `code`                 | quando                                         | fecha a conexão? (código WS) |
|------------------------|------------------------------------------------|------------------------------|
| `bad_message`          | JSON inválido, `type` ausente ou inesperado    | não; sim (1002) se for a 1ª mensagem |
| `hello_timeout`        | nenhum `hello` em 5 s                          | sim (1008)                   |
| `unsupported_protocol` | versão de `protocol` não suportada             | sim (1002)                   |
| `unsupported_audio`    | formato de áudio diferente do aceito           | sim (1003)                   |
| `unauthorized`         | token ausente ou inválido                      | sim (1008)                   |
| `audio_before_ready`   | frame binário antes do handshake               | sim (1008)                   |
| `bad_audio`            | frame binário com número ímpar de bytes        | não (frame descartado)       |
| `internal`             | erro inesperado no servidor                    | depende                      |

## Encerramento

O cliente pode fechar a conexão a qualquer momento. Uma frase em andamento é descartada.

## Histórico de versões

| versão | fase | mudanças            |
|--------|------|---------------------|
| 1      | 1    | versão inicial      |
