# ADR-006: Protocolo v2: respostas faladas, half-duplex e medição de ponta a ponta

- **Status:** Aceito
- **Data:** 2026-10-11
- **Evolui:** [ADR-003](0003-protocolo-websocket.md) (a base continua: WebSocket, PCM binário + JSON, token)

## Contexto

Na Fase 2 a Agatha passa a responder em voz. O protocolo v1 só levava áudio do cliente para o
servidor e transcrições de volta. Agora ele também precisa:

- **levar o áudio da resposta** do servidor para o cliente, em streaming, para a fala começar
  assim que o primeiro trecho estiver sintetizado (ADR-004 e ADR-005);
- **evitar que a Agatha ouça a si mesma.** Com caixas de som, o microfone capta a própria resposta
  e o VAD a trataria como uma nova fala. O Reinaldo usa headset hoje, mas não quer depender dele.
  Interromper a Agatha no meio da fala (barge-in) exigiria cancelamento de eco, e fica para depois;
- **medir a meta real** (fim da fala → primeiro som, menos de 2 s) de ponta a ponta. Os tempos do
  servidor não incluem a rede nem o buffer de reprodução do cliente, e os relógios das duas
  máquinas não são sincronizados.

## Decisão

1. **Half-duplex, garantido pelo servidor.** Cada sessão tem um estado: ouvindo → respondendo →
   ouvindo. Depois de uma transcrição com texto, o servidor **ignora o áudio recebido** até o
   cliente enviar `playback_end`. Então ele zera o VAD e volta a ouvir. O cliente também **para de
   enviar o microfone** durante a resposta, para economizar tráfego e evitar eco, mas a garantia
   é do servidor. Há um prazo de segurança: se o `playback_end` não chegar até 15 s depois da
   duração do áudio, o servidor volta a ouvir sozinho.

2. **Respostas em streaming pelo mesmo WebSocket:**
   - o servidor envia `response_start`, depois `response_text` (o texto de cada trecho, para exibir)
     intercalado com o áudio;
   - antes do primeiro áudio vai um `audio_start`, que declara o formato;
   - o áudio segue em frames binários de até 4096 amostras;
   - depois vêm `audio_end` e, por fim, `response_end`, com o texto completo e os tempos do servidor.

   O áudio da resposta usa a taxa nativa da voz (22 050 Hz, ADR-005), também anunciada no `ready`.

3. **Medição de ponta a ponta no relógio do cliente:**
   - o servidor informa no `vad end` o `speech_end_ms`, a posição no fluxo de áudio do fim da fala;
   - o cliente, que anotou quando capturou cada bloco, calcula o tempo até o primeiro áudio da
     resposta chegar à saída de som e envia o resultado (`e2e_ms`) no `playback_end`;
   - para as posições baterem, o servidor **conta todas as amostras recebidas**, inclusive as
     ignoradas durante uma resposta.

4. **Só a versão 2 é aceita.** Cliente e servidor são atualizados juntos e há um usuário só.
   Clientes v1 recebem `unsupported_protocol`.

A especificação completa está em [`docs/protocol.md`](../protocol.md).

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Full-duplex com barge-in | exige cancelamento de eco (ou headset obrigatório) e cancelamento de geração; fica para uma fase futura |
| Só o cliente silencia o microfone | um cliente com defeito ou um atraso na rede faria a Agatha responder a si mesma; o servidor precisa da garantia |
| Áudio da resposta num canal separado (HTTP, segundo WebSocket) | mais conexões e sincronização entre texto e áudio, sem ganho na LAN |
| Reamostrar a resposta para 16 kHz | perde qualidade e gasta CPU; o cliente reproduz qualquer taxa declarada |
| Medir a latência total só no servidor | não inclui rede nem buffer de reprodução; sincronizar relógios (NTP) dá erro da mesma ordem que o que se quer medir |

## Consequências

**Positivas**

- A Agatha não responde a si mesma, com ou sem headset.
- O áudio começa a tocar com o primeiro trecho, sem esperar a resposta inteira.
- A meta de latência passa a ser medida de verdade, de ponta a ponta. Comparar o `e2e_ms` com o
  `first_audio_ms` do servidor mostra quanto custam a rede e o buffer do cliente.
- O texto chega junto com o áudio, então o cliente pode exibir o que está sendo dito.

**Negativas / riscos**

- **Não dá para interromper a Agatha:** o que for dito enquanto ela pensa ou fala é descartado.
  Respostas curtas (prompt v2) reduzem o problema. Barge-in fica para depois.
- **Mais estado no servidor:** sessão com estado, prazo de segurança e contagem de amostras
  ignoradas. Precisa de testes específicos.
- **Ruído e conversas ao redor continuam sendo captados enquanto ela ouve.** O half-duplex não
  resolve isso; a wake word (Fase 3) resolve.
- **Quebra de compatibilidade com a v1:** o cliente precisa ser atualizado junto com o servidor.
