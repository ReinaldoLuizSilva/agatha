# ADR-027: Transcrição antecipada — o STT começa antes de confirmar o fim da fala

- **Status:** Aceito
- **Data:** 2026-10-23
- **Fase:** 11, item 1 (latência de perguntas longas; escopo aprovado em 22/10)
- **Complementa:** [ADR-002](0002-stt-faster-whisper-small-gpu.md) (faster-whisper `small`),
  [ADR-003](0003-protocolo-websocket.md) (Silero VAD e fim da fala em 500 ms),
  [ADR-007](0007-ajustes-latencia-conversa.md)

## Contexto

O item 1 da Fase 11 era a **transcrição parcial**: transcrever o que já foi dito enquanto ele fala,
para no fim só faltar o pedaço final. A motivação vinha da Fase 2, em que perguntas longas tinham
STT de 1,0–1,4 s.

Antes de implementar, os tempos de 398 transcrições reais (registro de turnos, Whisper na GPU)
mostraram outra coisa:

| fala | Whisper p50 | p90 | palavras p50 |
|---|---|---|---|
| 0–2 s (131) | 362 ms | 413 ms | 3 |
| 2–4 s (221) | 383 ms | 434 ms | 7 |
| 4–7 s (44) | 440 ms | 490 ms | 12 |

O tempo do Whisper quase não cresce com a fala: o faster-whisper completa o áudio até 30 s e o
encoder custa o mesmo para qualquer frase; só a decodificação cresce, ~80 ms de 2 para 12 palavras.
Transcrever por partes pagaria o encoder de novo no pedaço final, e o ganho máximo seria ~80 ms. O
que pesa é outra coisa: **os ~510 ms de silêncio** que o VAD espera para confirmar o fim da fala
(`min_silence_ms`), em toda pergunta, antes de o Whisper começar.

## Decisão

1. **Transcrever já com `early_ms` (192 ms) de silêncio.** O `Segmenter`
   ([`vad.py`](../../server/agatha_server/vad.py)) emite `SpeechPause` (interno, não vai ao cliente)
   quando a frase confirmada chega a 192 ms de silêncio, com o áudio até ali. A sessão
   ([`app.py`](../../server/agatha_server/app.py)) manda esse áudio para o STT na hora.
2. **No fim da fala** (500 ms, `SpeechEnd`), se a frase fechou no mesmo silêncio, usa o texto da
   antecipação, que em geral já está pronto. Se ele voltou a falar, a pausa seguinte gera outra
   antecipação e a anterior é descartada. Corte por tamanho máximo não usa a antecipação. Se a
   antecipação falhar, transcreve de novo o áudio inteiro.
3. O fim da fala continua em 500 ms e o barge-in não muda. Só o texto fica pronto ~320 ms mais cedo.
   A espera por continuação de frase cortada (`continuation_ms`) conta a partir do texto pronto: ela
   passou de 700 para 1000 ms, para a janela continuar em ~1,6 s depois do fim da fala (veja o teste
   no Windows).
4. `AGATHA_VAD_EARLY_MS` ajusta o valor (0 desliga). O `transcript` traz `"early": true` nos tempos,
   e o `server.log` traz "(antecipado)".

## Resultados

[`bench/stt_early_bench.py`](../../bench/stt_early_bench.py): 104 gravações da voz dele (40 frases
de jargão e 64 comandos com o headset), 105 frases, mesmo VAD e mesmo vocabulário do servidor, no CPU
(os textos são os da GPU; os tempos da GPU vêm do registro de turnos). Compara o texto com o áudio
até K ms de silêncio contra o texto com o áudio até o fim confirmado.

| começar o STT aos | ganho | WER | jargão | texto igual ao normal | transcrições descartadas |
|---|---|---|---|---|---|
| 500 ms (antes) | — | 26,6% | 147/173 | — | — |
| 96 ms | ~416 ms | 26,4% | 147/173 | 86/104 | 31 (em 24 falas) |
| 160 ms | ~352 ms | 27,0% | 145/173 | 88/104 | 13 (11) |
| **192 ms** | **~320 ms** | **26,4%** | **146/173** | **89/104** | **11 (10)** |
| 256 ms | ~256 ms | 26,2% | 147/173 | 89/104 | 6 (6) |

- A precisão não muda: em ~15 de 104 frases o texto sai diferente, com erros para os dois lados.
  O Whisper varia um pouco com o tamanho do silêncio no fim, e o WER fica igual. O WER alto vem dos
  comandos curtos gravados com o headset para o detector do nome (gravações ruins de propósito).
- Pausas no meio da fala: 43, p50 96 ms, máximo 480 ms. Com 192 ms, ~1 fala em 10 gasta uma
  transcrição à toa (~400 ms de GPU enquanto ele ainda fala, quando o LLM está parado).
- O ganho vem de o Whisper (~400 ms) começar aos 192 ms em vez dos 512: o texto fica pronto por
  volta dos 600 ms em vez dos ~910 ms. Com 96 ms ele estaria pronto antes do fim confirmado, e o ganho
  extra (~100 ms) custaria 3 vezes mais descartes.

### Teste no Windows (23/10, headset, 12 falas)

- As 12 falas usaram a antecipação. Do fim da fala ao texto pronto: 517–648 ms (antes ~910 ms).
  1º som 1,49–2,30 s (curtas: 1,49–2,14 s); as perguntas longas de duas frases ("a tablespace USERS
  está cheia? E quais sessões estão bloqueadas?") saíram inteiras, com 645–648 ms até o texto.
- Barge-in: "Agatha para" parou a resposta (disparo 453 ms depois do começo da fala), 0 à toa.
- **Regressão encontrada:** "Agatha, qual é a..." (hesitação) foi segurada como incompleta, mas a
  janela de continuação conta a partir do texto pronto. Com o texto ~320 ms mais cedo, ela caiu de
  ~1,6 s para ~1,25 s depois do fim da fala, e a continuação chegou tarde (resposta "não entendi").
  Corrigido: `continuation_ms` 700 → 1000.

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Transcrição por partes durante a fala (o plano original) | o encoder do Whisper custa o mesmo para 2 ou 12 s de fala: o pedaço final custaria quase o mesmo que a frase inteira (ganho ≤ ~80 ms) e os cortes arriscam o jargão |
| Diminuir `min_silence_ms` | corta frases em pausas naturais (Fase 6: 2 cortes em 74 falas já com 500 ms) |
| Antecipar também o LLM | o prefixo do cache não muda (ADR-007), mas uma resposta cancelada gasta GPU e complica o half-duplex; o ganho a mais seria pequeno, porque o STT antecipado já termina quase junto com o fim confirmado |
| K = 256 ms (menos descartes) | ~64 ms a menos de ganho; 192 ms tem o mesmo WER e só 11 descartes em 104 falas |

## Consequências

**Positivas**

- ~320 ms a menos do fim da fala até o texto pronto em **toda** pergunta, curta ou longa, sem mudar
  a precisão nem o ponto de fim da fala.

**Negativas / riscos**

- Uma transcrição a mais na GPU em ~1 de cada 10 falas (pausa de 192–500 ms no meio). Com o barge-in,
  ela pode disputar a GPU com o LLM ainda gerando, como já acontecia com o STT normal.
- O texto antecipado não tem os últimos ~320 ms de silêncio. No bench isso não mudou o WER, mas o
  registro de turnos (`"early": true`) permite comparar no uso.
