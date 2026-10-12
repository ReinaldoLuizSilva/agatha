# ADR-015: Cancelamento de eco linear no cliente e detector treinado com eco

- **Status:** Aceito
- **Data:** 2026-10-12
- **Complementa:** [ADR-014](0014-wake-word-no-cliente-para-barge-in.md) (o detector do barge-in)

## Contexto

O barge-in (ADR-014) precisa ouvir "Agatha" enquanto ela fala pelas **caixas de som do notebook**.
Nas 25 gravações de eco (`python -m agatha_client.record`, parte `eco`, com o áudio exato que
saiu nas caixas ao lado):

- o eco chega ao microfone com **atraso fixo de 289 ms** (stream duplex do gravador);
- a voz do Reinaldo chega **de 0 a 10 dB abaixo do eco**: as caixas estão coladas ao microfone e
  ele fala a ~50 cm;
- sem cancelamento de eco, o detector acha o nome em só 23–31% das interrupções.

O plano era usar o AEC3 do WebRTC (o do Chrome), disponível para Python no Windows pelo SDK da
LiveKit e pelo `pywebrtc-audio`.

## Resultados

[`bench/aec_bench.py`](../../bench/aec_bench.py) e `client/wakeword/train.py --eval`; tabelas em
`bench/results/aec-*` e `bench/results/wake-train-*`. ERLE = quanto eco sai nas falas caladas.

| cancelamento | ERLE | acerto nas interrupções (detector do ADR-014) |
|---|---|---|
| nenhum | — | 23–31% |
| AEC3 do WebRTC (LiveKit; com supressão de ruído) | ~30 dB | 31% (15%) |
| `pywebrtc-audio` (AEC3) | ~31 dB | 31% |
| linear (filtro ideal, calculado depois sobre a gravação inteira) | ~10 dB | 62% |
| linear adaptativo do cliente, recomeçando em cada fala | ~11,5 dB | 69% |
| **linear adaptativo do cliente, filtro persistente** | ~13,5 dB | 62% |
| **linear + detector treinado com eco simulado** (2 dobras, limiar 0,5 / 0,9) | ~13,5 dB | **85% / 77%** |

- O **AEC3 apaga a voz junto com o eco**: o supressor não linear corta forte quando duas pessoas
  falam ao mesmo tempo, e a voz do Reinaldo, mais baixa que o eco, vai junto. Mesmo a transcrição
  do áudio limpo pelo AEC3 sai sem nenhum "Agatha".
- O **cancelamento só linear** tira menos eco (a distorção das caixas do notebook não é linear),
  mas preserva a voz e chega ao acerto do filtro ideal (62–69%; com 13 interrupções, a diferença
  entre recomeçar o filtro e mantê-lo está dentro do ruído). O persistente tira mais eco, que é
  o que importa para não disparar com a voz dela.
- Treinar o detector **na condição real** (as falas do Reinaldo somadas a trechos reais de eco, no
  volume medido, e passadas pelo mesmo AEC) levou o acerto com eco de 62% para 85%, sem piorar o
  acerto sem eco (90/83/75%).

## Decisão

1. **Cancelamento de eco próprio, só linear** (`client/agatha_client/aec.py`): filtro adaptativo em
   blocos no domínio da frequência (PBFDAF, overlap-save, como o do Speex), 128 ms de cauda,
   atraso fixo informado, adaptação normalizada e congelada em silêncio da referência ou quando
   alguém fala por cima. Só numpy, latência fixa de 16 ms, testado com eco sintético.
2. **O filtro persiste durante a sessão** do cliente: ele aprende a sala nas primeiras respostas e
   não recomeça do zero a cada uma.
3. **O detector é treinado com eco simulado** (`client/wakeword/train.py`): cada fala com o nome
   ganha 4 cópias por cima de trechos reais de eco das gravações caladas, com a voz de 0 a 10 dB
   abaixo do eco, depois do AEC. A voz da Agatha que sobra depois do AEC entra como negativo.
4. **No cliente, microfone e saída de som no mesmo stream (duplex)**, como no gravador: a
   referência fica alinhada com o microfone e o atraso é constante. O atraso (289 ms no notebook
   do Reinaldo) é medido no início da sessão; o filtro começa 16 ms antes dele, porque não modela
   eco que chegue antes do atraso informado. A taxa do stream (16 kHz, ou 48 kHz com reamostragem
   para não cortar os agudos da voz dela) fica para a implementação do barge-in.

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| AEC3 do WebRTC (LiveKit ou `pywebrtc-audio`) | medido: apaga a voz de quem fala por cima; o barge-in não funciona |
| AEC do sistema (Windows, modo "comunicações") | sem acesso pelo PortAudio, sem controle nem medição, e o mesmo problema de supressão |
| Speex (`speexdsp`) | sem pacote para o Windows nem para Python 3.12 |
| Exigir headset | o Reinaldo usa as caixas do notebook; resolver isso é o objetivo |
| Abaixar o volume dela quando alguém fala (ducking) | o nome é a primeira palavra: quando a fala é detectada, metade do "Agatha" já passou |

## Consequências

**Positivas**

- Barge-in viável com as caixas do notebook: 85% (limiar 0,5) ou 77% (0,9) das interrupções.
- Sem dependência nova no cliente para o eco (só numpy, que já é dependência); ~16 ms de latência.

**Negativas / riscos**

- **A própria voz dela ainda dispara às vezes**: 1 a 3 disparos em ~90 s de fala dela calada
  (limiar 0,9 a 0,5). Um caso é uma resposta em que ela fala uma palavra parecida com o nome. Um
  falso disparo cortaria a resposta; o barge-in precisa de uma confirmação barata (próxima etapa):
  pausar a fala, conferir pela transcrição no servidor e retomar se não era com ela.
- **Treinar com eco aumenta os falsos disparos em áudio comum**: na validação do openWakeWord
  (10,7 h), de ~3/h para ~9/h nas dobras e de 10 a 23/h no modelo final (limiar 0,99 a 0,5),
  que usa o dobro de exemplos com eco. Como o detector só fica ligado enquanto ela fala (ADR-014),
  isso vale só para esses minutos: algo como uma interrupção falsa por dia vinda de TV ou conversa
  ao fundo. Daí a confirmação: o cliente pausa a fala e o servidor confere pela transcrição.
- **Amostra pequena**: 13 interrupções reais e 12 falas caladas; os números variam ±10–15%.
- **Depende do atraso medido e do volume**: outro notebook, outro volume ou outro posicionamento
  pedem nova medição; o volume muito alto aumenta a distorção que o filtro linear não remove.
- O stream duplex usa uma taxa só para entrada e saída: ou a resposta toca a 16 kHz (perde os
  agudos acima de 8 kHz), ou o cliente reamostra (mais CPU e código).
