# ADR-016: Barge-in — pausa no cliente, confirmação pela transcrição no servidor

- **Status:** Aceito (testado no Windows em 13/10; ver "Teste real")
- **Data:** 2026-10-12
- **Complementa:** [ADR-006](0006-protocolo-v2-half-duplex.md) (half-duplex),
  [ADR-014](0014-wake-word-no-cliente-para-barge-in.md) (detector no cliente) e
  [ADR-015](0015-cancelamento-de-eco-linear.md) (cancelamento de eco)

## Contexto

Até a Fase 4 a conversa era half-duplex (ADR-006): enquanto a Agatha fala, o microfone nem é
enviado, e uma fala por cima é perdida. A Fase 5 quer interromper a Agatha pelo nome, com a meta
de **nome → silêncio em menos de 300 ms**.

As etapas anteriores deram as peças:

- um detector de "Agatha" no cliente, treinado com a voz do Reinaldo (ADR-014);
- um cancelamento de eco linear que tira a voz dela do microfone sem apagar a dele (ADR-015).

O detector acerta 85% (limiar 0,5) ou 77% (limiar 0,9) das interrupções reais, mas **dispara à
toa**: 1 a 3 vezes em ~90 s da própria voz dela, e de 10 a 23 vezes por hora em áudio comum (TV,
conversa). Um falso disparo não pode custar a resposta inteira.

A transcrição no servidor (ADR-008) é confiável (98% de acerto), mas lenta para isso: só sai
depois que a frase termina (500 ms de silêncio + STT).

## Decisão

1. **Pausar no cliente, decidir no servidor.** Quando o detector dispara, o cliente **pausa** a
   fala na hora (não descarta nada) e envia `barge_in` com quanto já tinha tocado. O servidor
   passa ao estado "confirmando", volta a ouvir e decide pela primeira frase transcrita:
   - chamou a Agatha ou mandou parar → `barge_in_end` "stop": a resposta é cancelada;
   - não era com ela, ou nenhuma fala começou em 2 s → "resume": ela continua de onde parou.

   A pausa atende à meta de latência. A transcrição garante que um falso disparo custe só uma
   pausa curta, e não a resposta.
2. **O cliente manda o áudio desde ~1 s antes do nome**, guardado e já sem eco, para o servidor
   transcrever a frase inteira ("Agatha, para"). Com isso o nome já está no áudio, e o prazo para
   a fala começar pode ser curto (2 s). O que vem antes do nome na transcrição (sobra de eco) é
   ignorado (`wake.from_name`).
3. **Parar ou perguntar** (`wake.interruption_kind`): até 3 palavras começando por "para",
   "chega", "cancela", "silêncio"... (ou só o nome) é comando. Não vai ao LLM, e o servidor fica
   no modo conversa. O resto é pergunta nova e é respondida em seguida. "Agatha, para que serve um
   backup?" é pergunta, porque tem mais de 3 palavras.
4. **O histórico fica só com o que foi ouvido.** O servidor guarda o início de cada trecho de
   texto no áudio. Os trechos que começaram antes de `played_ms` substituem a resposta no
   histórico. O começo do prompt não muda (ADR-007): o llama.cpp só reprocessa o fim do último
   turno.
5. **Cancelar de verdade**: o gerador da resposta é fechado (`aclose`), então o llama.cpp para de
   gerar e o TTS para de sintetizar.
6. **No cliente, microfone e caixas no mesmo stream (duplex) a 16 kHz** (ADR-015, decisão 4). O
   cliente pede a resposta a 16 kHz no `hello` (`output_audio`) e o servidor reamostra cada frase
   inteira (filtro FIR + interpolação, `server/agatha_server/audio.py`). O stream usa latência
   baixa, porque a pausa só se ouve depois que a placa toca o que já recebeu.
7. **O atraso do eco é medido pela própria fala dela**, nos primeiros 2 s de cada sessão,
   por correlação cruzada GCC-PHAT (`aec.estimate_delay`), sem tocar bipe. Nas 25 gravações de eco,
   2 s bastaram: 288,2–289,8 ms em todas (o valor conhecido é 289), com confiança 19–29, contra 4,5
   sem eco. O corte de confiança é 10. Antes de medir, o detector não dispara. Se depois de 3
   tentativas não houver eco (fone de ouvido), ele passa a ouvir direto.
8. **O detector só pode disparar durante uma resposta** (de `response_start` ao `playback_end`),
   com 1,5 s de intervalo entre disparos. **Limiar padrão 0,9**, ajustável com
   `--barge-threshold`: com a confirmação, errar para menos (77%) incomoda menos que pausas à toa.
9. **Opcional no cliente**: sem o extra `[barge-in]` (openWakeWord) ou sem os modelos, o cliente
   avisa e funciona como antes (`--no-barge-in` força isso).

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Interromper direto no disparo do cliente, sem confirmar | um falso disparo (10–23/h em áudio comum, 1–3 em 90 s da voz dela) jogaria fora a resposta |
| Só a transcrição no servidor, sem detector no cliente | o texto só sai depois da frase acabar: o nome → silêncio passaria de 1 s |
| Abaixar o volume dela ao detectar voz (ducking) | o nome é a primeira palavra; quando a voz é detectada, metade dele já passou (ADR-015) |
| Stream duplex a 48 kHz com reamostragem no cliente | mais CPU e código no cliente; 16 kHz perde os agudos acima de 8 kHz, a reavaliar no teste real |
| Medir o atraso com um bipe no início da sessão | incomoda a cada reconexão; a fala dela mede igual (25/25 nas gravações) |
| Atraso fixo de 289 ms (o do gravador) | muda com a latência do stream; o cliente usa latência baixa, diferente da do gravador |

## Teste real (13/10)

Três rodadas no notebook do Reinaldo (caixas e "Microfone Externo (Realtek)"), com uma linha de
diagnóstico por resposta no cliente: nota máxima do detector, eco antes e depois do AEC (sem
guardar áudio). Fim da fala → primeiro som continuou em 1,85–2,2 s (rede/reprodução 150–177 ms,
contra 131 ms com streams separados). O atraso do eco foi medido em 162 ms em todas as sessões
(confiança 27–77).

1. **1ª rodada: o detector só disparava no fim da fala dela** (`played_ms` 9575 de ~9,7 s) e nem
   isso em outras três respostas longas.
2. **O diagnóstico mostrou o AEC piorando o sinal**: eco no microfone de −54 a −67 dBFS (quase
   nada; nas gravações de 12/10 ele vinha acima da voz do Reinaldo) e saída do AEC 22–44 dB
   **acima** do microfone. Reproduzido em simulação: o eco começa forte, o Windows (ou o driver
   Realtek) passa a cancelá-lo, e o filtro continua subtraindo o eco antigo; o detector de fala
   por cima tomava isso por fala e não soltava (depois de `dtd_hold_s` adaptava um único bloco).
   **Correção:** saída mais alta que o microfone = filtro errado, não fala por cima; ele adapta
   sem congelar e, enquanto isso, devolve o microfone cru. Depois do congelamento máximo, o erro
   atual vira o novo normal.
3. **Bug no servidor:** uma frase dita na confirmação ficou 1,9 s no STT, o prazo de 2 s retomou
   a resposta e depois a frase foi respondida no modo conversa. **Correção:** o prazo espera a
   transcrição pendente, e frases de uma confirmação já retomada são descartadas.
4. **3ª rodada:** "AEC tirou 0 dB" (não há eco para tirar: o sistema já cancela) e o barge-in
   **funciona quando o microfone fica afastado das caixas** ("Agatha para" pausou aos 2,4 s e
   interrompeu). Com o microfone perto, a nota fica em 0,00: o cancelamento do sistema apaga a voz
   do Reinaldo junto com o eco quando os dois chegam juntos, o mesmo efeito medido no AEC3 (ADR-015).
   Quando o detector perde o nome, o "Agatha, para" dito no fim da fala dela chega depois como
   frase comum e vai ao LLM ("não entendi").

Decisão do Reinaldo: o barge-in fica como está e melhora aos poucos. Próximo teste: desligar as
melhorias de áudio do microfone no Windows (o eco volta e o AEC linear, que preserva a voz, assume).

## Consequências

**Positivas**

- Dá para interromper a Agatha pelo nome, e um falso disparo custa uma pausa curta, não a
  resposta.
- Os comandos de parar não passam pelo LLM; a pergunta nova é respondida sem repetir o nome.
- O histórico continua coerente com o que o Reinaldo ouviu, sem quebrar o cache do prompt.
- O protocolo continua na versão 2: os campos e mensagens novos são opcionais, e o cliente antigo
  segue funcionando.

**Negativas / riscos**

- **Depende do processamento de áudio do Windows**: com as melhorias do microfone ligadas, o
  barge-in só funciona com o microfone afastado das caixas (Teste real, item 4). Ainda falta
  testar com elas desligadas.
- **Ainda não medidos**: nome → silêncio (meta < 300 ms), frequência dos falsos disparos durante
  a fala dela e o custo de CPU do detector no notebook (ele roda o tempo todo, mas só age durante
  a resposta).
- **O ajuste do cancelamento de eco não foi validado nas gravações reais**: o detector novo de
  fala por cima (`dtd_ratio` 4) manteve o acerto (85% com 0,5 e 0,9) mas tirou menos eco (ERLE
  ~5,7 dB contra ~13,5), e a varredura de `dtd_ratio` espera o memtest (queda do servidor em
  12/10, suspeita de RAM). Com o eco já cancelado pelo Windows, perdeu prioridade.
- **A voz a 16 kHz perde os agudos** acima de 8 kHz no modo barge-in. Se incomodar, a
  alternativa é o stream a 48 kHz (decisão 6).
- Num falso disparo sem ninguém falando, ela fica calada até 2 s. Se a sobra de eco no áudio
  enviado parecer fala, a transcrição decide, e isso leva ~1–1,5 s.
- **Parar ou perguntar** é decidido por regra simples: "Agatha, espera aí" é tratado como
  pergunta e vai ao LLM.
