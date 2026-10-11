# Protocolo cliente ↔ servidor

Especificação do protocolo WebSocket entre o cliente (PC Windows) e o servidor (`agatha-srv`).
As decisões e os motivos estão no [ADR-003](adr/0003-protocolo-websocket.md) (base) e no
[ADR-006](adr/0006-protocolo-v2-half-duplex.md) (v2: respostas faladas e half-duplex).

- **Versão atual:** 2 (Fase 2: conversa falada). O servidor só aceita a versão 2.
- **Endereço:** `ws://<servidor>:8765/`

## Visão geral

Uma única conexão WebSocket por cliente, com dois tipos de frame:

- **Binário:** áudio PCM. Do cliente para o servidor vai o microfone (16 kHz); do servidor para o
  cliente vai a resposta falada (22 050 Hz). O formato de cada sentido é declarado no handshake.
- **Texto:** mensagens de controle em JSON, nos dois sentidos, sempre com o campo `type`.

A conversa é **half-duplex**: enquanto a Agatha prepara e fala uma resposta, o servidor ignora o
microfone. Ele volta a ouvir quando o cliente avisa que terminou de tocar (`playback_end`).

```
cliente                                          servidor
   │── {"type":"hello", ...} ──────────────────────►│
   │◄──────────────────── {"type":"ready", ...} ────│
   │── [PCM 16 kHz] ... ───────────────────────────►│  ouvindo
   │◄──────────── {"type":"vad","event":"start"} ───│
   │── [PCM 16 kHz] ... ───────────────────────────►│
   │◄──────────── {"type":"vad","event":"end"} ─────│
   │◄──────────── {"type":"transcript", ...} ───────│  pensando (microfone ignorado)
   │◄──────────── {"type":"response_start"} ────────│
   │◄──────────── {"type":"response_text"} ─────────│
   │◄──────────── {"type":"audio_start"} ───────────│  falando
   │◄──────────── [PCM 22 kHz] ... ─────────────────│
   │◄──────────── {"type":"response_text"} ─────────│
   │◄──────────── [PCM 22 kHz] ... ─────────────────│
   │◄──────────── {"type":"audio_end"} ─────────────│
   │◄──────────── {"type":"response_end"} ──────────│
   │   (termina de tocar)                            │
   │── {"type":"playback_end", ...} ───────────────►│  ouvindo de novo
```

## Handshake

A primeira mensagem do cliente **precisa** ser um `hello`, enviado em até 5 s após a conexão.
Antes do `ready`, o servidor rejeita qualquer áudio.

### `hello` (cliente → servidor)

```json
{
  "type": "hello",
  "protocol": 2,
  "client": "agatha-client/0.2 (win32)",
  "token": "<AGATHA_TOKEN>",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
}
```

| campo      | obrigatório | descrição                                                      |
|------------|-------------|----------------------------------------------------------------|
| `protocol` | sim         | versão do protocolo. Só `2` é aceita                           |
| `client`   | não         | identificação livre do cliente, usada só em log                |
| `token`    | sim         | segredo compartilhado (`AGATHA_TOKEN` no `.env` do servidor)   |
| `audio`    | sim         | formato do microfone. Só `pcm_s16le`, `16000`, `1` é aceito    |

### `ready` (servidor → cliente)

```json
{
  "type": "ready",
  "protocol": 2,
  "session": "3f9c2a71",
  "output_audio": {"encoding": "pcm_s16le", "sample_rate": 22050, "channels": 1},
  "wake": {"enabled": true, "words": ["agatha", "agata", "agatah"], "conversation_s": 10},
  "vad": {
    "threshold": 0.5,
    "min_silence_ms": 500,
    "pre_roll_ms": 300,
    "min_speech_ms": 250,
    "max_segment_s": 25,
    "continuation_ms": 700
  }
}
```

`output_audio` é o formato das respostas faladas; a taxa depende da voz do TTS. `wake` descreve a
wake word ([ADR-008](adr/0008-wake-word-pela-transcricao.md)): se está ligada, as palavras aceitas e
a duração do modo conversa. `vad` informa os parâmetros em uso, só para referência e log.

## Áudio do microfone (cliente → servidor)

- Frames **binários** de PCM com sinal, 16 bits, little-endian (`s16le`), 16 kHz, mono.
- Tamanho recomendado: **512 amostras** (32 ms, 1024 bytes), o bloco nativo do VAD. O servidor
  aceita qualquer tamanho e reagrupa, mas o número de bytes precisa ser par.
- O cliente envia no ritmo em que captura e **para de enviar** enquanto uma resposta está em
  andamento (de `response_start` até mandar o `playback_end`).
- **Posição no fluxo:** o servidor conta *todas* as amostras recebidas na sessão, inclusive as que
  ele ignora durante uma resposta. Os campos `audio_ms` e `speech_end_ms` se referem a essa
  contagem, que o cliente reproduz contando as amostras que enviou.

## Eventos de fala

### `vad`

```json
{"type": "vad", "event": "start", "segment": 1, "audio_ms": 1216}
{"type": "vad", "event": "end", "segment": 1, "audio_ms": 3840, "speech_end_ms": 3328, "reason": "silence"}
```

| campo           | descrição                                                                 |
|-----------------|---------------------------------------------------------------------------|
| `event`         | `start`: fala confirmada. `end`: fim da frase                              |
| `segment`       | número sequencial da frase na sessão, começando em 1                       |
| `audio_ms`      | no `start`, o início real da fala; no `end`, onde o fim foi detectado      |
| `speech_end_ms` | só no `end`: fim do último bloco com fala. É o "fim da fala" da medição de ponta a ponta |
| `reason`        | só no `end`: `silence` ou `max_length`                                     |

O `start` só sai depois de `min_speech_ms` de fala. Trechos mais curtos são descartados sem evento.

### `transcript`

```json
{
  "type": "transcript",
  "segment": 1,
  "text": "Agatha, qual o tamanho da tablespace USERS?",
  "addressed": true,
  "timings": {"speech_ms": 2100, "endpoint_ms": 512, "queue_ms": 0, "stt_ms": 341, "total_ms": 853}
}
```

`addressed` diz se a fala era para a Agatha: o nome entre as 4 primeiras ou as 2 últimas palavras,
fala iniciada no modo conversa (até `conversation_s` depois da última resposta) ou continuação de
uma fala que era para ela. Se for `false`, não há resposta.

Os tempos são os mesmos da v1 (`total_ms` vai do último bloco com fala até o texto pronto). Se o
texto não estiver vazio e `addressed` for `true`, o servidor **inicia uma resposta** e passa a
ignorar o microfone. Texto vazio não gera resposta.

**Frase cortada por uma pausa:** se o texto parecer incompleto (termina em reticências, vírgula ou
numa palavra como "e", "de", "que", "para"), o servidor espera até `continuation_ms` por uma
continuação. Se a pessoa voltar a falar, a próxima transcrição é **juntada** à anterior e a
resposta (`response_start.segment`) se refere à última frase. Se não, responde ao que tem. Cada
parte continua gerando o seu `transcript`.

## Respostas

### `response_start`

```json
{"type": "response_start", "response": 1, "segment": 1}
```

`response` é o número sequencial da resposta na sessão; `segment` é a frase que a originou.

### `response_text`

```json
{"type": "response_text", "response": 1, "text": "Oi, Rei."}
```

Um trecho do texto da resposta, já limpo, para exibir. Chega trecho a trecho, cada um logo antes
do áudio correspondente.

### `audio_start` e áudio da resposta

```json
{"type": "audio_start", "response": 1, "encoding": "pcm_s16le", "sample_rate": 22050, "channels": 1}
```

Enviado uma vez por resposta, antes do primeiro frame de áudio. Em seguida vêm frames **binários**
de PCM no formato indicado, com até 4096 amostras cada (~186 ms a 22 050 Hz). Como há uma resposta
de cada vez, todo frame binário entre `audio_start` e `audio_end` pertence a ela.

### `audio_end`

```json
{"type": "audio_end", "response": 1}
```

### `response_end`

```json
{
  "type": "response_end",
  "response": 1,
  "reason": "done",
  "text": "Oi, Rei. Tudo certo por aqui.",
  "timings": {
    "stt_ms": 853,
    "llm_first_token_ms": 1121,
    "first_chunk_ms": 1402,
    "tts_first_ms": 38,
    "first_audio_ms": 1447,
    "audio_ms": 2310
  }
}
```

| campo    | descrição                                                             |
|----------|-----------------------------------------------------------------------|
| `reason` | `done` (resposta completa) ou `error` (falha no LLM ou no TTS)        |
| `text`   | texto completo da resposta, limpo                                     |

Tempos, em ms, **contados a partir do último bloco com fala** (o mesmo ponto do `speech_end_ms`),
no relógio do servidor:

| campo                | até quando                                          |
|----------------------|-----------------------------------------------------|
| `stt_ms`             | transcrição pronta                                  |
| `llm_first_token_ms` | primeiro token do LLM                               |
| `first_chunk_ms`     | primeiro trecho de texto pronto para o TTS          |
| `tts_first_ms`       | duração da síntese desse primeiro trecho (não acumulado) |
| `first_audio_ms`     | primeiro frame de áudio enviado                     |
| `audio_ms`           | duração total do áudio da resposta                  |

Uma resposta com `reason: "error"` pode não ter áudio (sem `audio_start`/`audio_end`).

### `playback_end` (cliente → servidor)

```json
{"type": "playback_end", "response": 1, "e2e_ms": 1650}
```

O cliente envia depois de tocar todo o áudio da resposta, ou logo após o `response_end` se não
houve áudio. O servidor então zera o VAD e volta a ouvir.

| campo    | obrigatório | descrição |
|----------|-------------|-----------|
| `response` | sim       | a resposta que terminou de tocar |
| `e2e_ms`   | não       | **fim da fala → primeiro som**, medido pelo cliente: do momento em que capturou a amostra na posição `speech_end_ms` até o primeiro áudio da resposta chegar à saída de som (incluindo a latência de saída informada pelo driver). `null` se não houve áudio |

Se o `playback_end` não chegar até 15 s depois da duração do áudio, o servidor volta a ouvir
mesmo assim.

## Erros

```json
{"type": "error", "code": "unauthorized", "message": "token inválido"}
```

| `code`                 | quando                                         | fecha a conexão? (código WS) |
|------------------------|------------------------------------------------|------------------------------|
| `bad_message`          | JSON inválido, `type` ausente ou inesperado    | não; sim (1002) se for a 1ª mensagem |
| `hello_timeout`        | nenhum `hello` em 5 s                          | sim (1008)                   |
| `unsupported_protocol` | versão de `protocol` diferente de 2            | sim (1002)                   |
| `unsupported_audio`    | formato de áudio diferente do aceito           | sim (1003)                   |
| `unauthorized`         | token ausente ou inválido                      | sim (1008)                   |
| `audio_before_ready`   | frame binário antes do handshake               | sim (1008)                   |
| `bad_audio`            | frame binário com número ímpar de bytes        | não (frame descartado)       |
| `internal`             | erro inesperado no servidor                    | depende                      |

## Encerramento

O cliente pode fechar a conexão a qualquer momento. Uma frase ou resposta em andamento é
descartada, e o histórico da conversa daquela sessão é perdido.

## Histórico de versões

| versão | fase | mudanças |
|--------|------|----------|
| 1      | 1    | versão inicial: microfone → transcrições |
| 2      | 2    | respostas faladas (`response_*`, `audio_*`, áudio binário do servidor), half-duplex com `playback_end`, `speech_end_ms` no `vad end`, `output_audio` no `ready`, medição de ponta a ponta no cliente |
| 2 (+)  | 2–3  | campos opcionais, compatíveis: `continuation_ms` no `ready.vad` (frase cortada), `wake` no `ready` e `addressed` no `transcript` (wake word) |
