# ADR-014: Wake word no cliente só para o barge-in, treinada com a voz do Reinaldo

- **Status:** Aceito (modelo retreinado em 20/10; ver "Retreino com o headset")
- **Data:** 2026-10-12
- **Complementa:** [ADR-008](0008-wake-word-pela-transcricao.md) (a chamada normal continua pela
  transcrição no servidor)

## Contexto

A Fase 5 quer duas coisas: interromper a Agatha enquanto ela fala (barge-in) e, se possível,
detectar o nome no próprio cliente, para o servidor não transcrever toda fala captada (ADR-008).

Para o barge-in, a transcrição no servidor é lenta: ela só sai depois que a frase termina (500 ms
de silêncio + STT). É preciso um detector no cliente, que reaja em centenas de milissegundos ao
"Agatha" dito por cima da voz dela. E o Reinaldo usa as **caixas de som do notebook**, então o
microfone capta a própria Agatha.

Não existe modelo pronto de "Agatha" (ADR-008). Para escolher, o Reinaldo gravou 141 falas com o
microfone do notebook (`python -m agatha_client.record`): 76 com o nome (perto, voz variada,
longe), 30 frases sem o nome escolhidas para confundir ("a gata", "agora", "a garrafa"), 25 com a
Agatha falando pelas caixas e 10 min de TV. Os falsos disparos também foram medidos em 1,3 h de
fala PT-BR (FLEURS) e nas 10,7 h de validação do openWakeWord. Detalhes e scripts:
[`bench/wake_bench.py`](../../bench/wake_bench.py), [`bench/wake_probe.py`](../../bench/wake_probe.py),
[`client/wakeword/`](../../client/wakeword/README.md); resultados em `bench/results/wake-*`.

### Resultados

Acerto por grupo e falsos disparos por hora em fala comum (eco calado + ambiente + FLEURS). O
treino foi avaliado em 2 dobras: as falas de teste nunca entram no treino daquela dobra. Eco ainda
**sem** cancelamento de eco.

| detector | perto | variado | longe | eco | "a gata" e cia. | falsos/h | falsos/h (validação) |
|---|---|---|---|---|---|---|---|
| **STT do servidor (ADR-008)** | 98% | 83% | 100% | 15% | 5/30 | 2,7 | — |
| Vosk PT-BR pequeno, gramática `["agatha", "[unk]"]` (limiar 0,7) | 98% | 46% | 75% | 31% | 9/30 | 22,3 | — |
| Vosk PT-BR pequeno, vocabulário livre | 5% | 0% | 0% | 0% | 0/30 | 0 | — |
| **openWakeWord treinado (limiar 0,5)** | 90% | 79% | 75% | 27% | 5/30 | 2,7 | 3,0 |
| openWakeWord treinado (limiar 0,98) | 90% | 71% | 67% | 23% | 5/30 | 0,7 | 1,1 |

O que se aprendeu no caminho:

- **Vosk** confunde "a gata" e "Agatha" (em PT-BR quase só o acento muda) e perde a voz variada
  (baixinho, rápido, cansado). Descartado.
- **openWakeWord**: o embedding de áudio pré-treinado separa a voz do Reinaldo de fala comum. O
  treino é só um classificador pequeno (16 × 96 → 128 → 128 → 1), no CPU, sem parar a Agatha.
- **Amostras sintéticas não ajudaram.** "Agatha" em 4 vozes PT-BR do Piper (16 mil janelas) dobrou
  os falsos disparos sem melhorar o acerto na voz real. O treino usa só as gravações dele (com
  ruído, reverberação, volume e velocidade aleatórios); os sintéticos ficam como opção desligada.
- **O que derrubou os falsos disparos** (de 10–20/h para ~1–3/h na validação): peso dos negativos
  crescendo até 100 e mineração de negativos difíceis em outra fatia das 2.000 h do ACAV100M.
- **Bug achado no caminho:** com `agatha-srv` no vocabulário, o Whisper escreveu "Agatha, chega"
  como "Agatha-srv" e a fala era ignorada. O `wake.py` passou a separar palavras com hífen.

## Decisão

1. **A chamada normal continua pela transcrição no servidor** (ADR-008). O detector do cliente
   acerta menos (90/79/75% contra 98/83/100%); usá-lo como porteiro do áudio perderia de 10 a 25%
   das chamadas. O ganho (GPU ociosa, privacidade) não compensa hoje.
2. **O detector do cliente serve para o barge-in**: enquanto a Agatha fala, o cliente procura
   "Agatha" no microfone, depois do cancelamento de eco (etapa seguinte da Fase 5), e interrompe.
   Um falso disparo aqui só corta uma resposta; com ~1–3/h de fala comum e poucos minutos de fala
   dela por dia, isso é raro.
3. **Modelo**: openWakeWord (melspectrograma + embedding pré-treinados, ONNX) + classificador
   próprio treinado com a voz do Reinaldo, exportado em
   `/dados/agatha/models/wakeword/agatha.onnx` (fora do git), no mesmo formato dos modelos do
   openWakeWord (entrada 16 × 96). O cliente usa o `openwakeword` com `onnxruntime` no Windows.
4. **Voz da Agatha só nos negativos.** Se ela disser o próprio nome numa resposta, o detector não
   pode interrompê-la.
5. **Treino sempre com teto de memória** (`systemd-run --user -p MemoryMax=5G`) e prioridade
   baixa: um teste sem teto jogou o LLM no swap e o primeiro áudio foi a 34 s até se recuperar.

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Porcupine (Picovoice), que treina em português | fechado e com chave validada online; vai contra o "100% local" e o portfólio aberto |
| Vosk com gramática | medido: perde a voz variada e dispara em "a gata", "agora", "a batata" |
| Pipeline oficial de treino do openWakeWord | dependências de 2022 (PyTorch 1.13, TensorFlow 2.8), notebook quebrado desde 2023, gera amostras em inglês; o treino próprio (`client/wakeword/train.py`) tem ~550 linhas |
| Treinar com a GPU | desnecessário: o classificador é pequeno e treina no CPU sem derrubar a Agatha |
| Barge-in pela transcrição no servidor | a transcrição só sai depois da frase terminar (~1 s depois do nome) e disputa a GPU com o LLM e o TTS no meio da resposta |
| Detector do cliente também como porteiro (só envia áudio depois do nome) | perderia 10–25% das chamadas hoje; reavaliar com mais dados |

## Retreino com o headset e features em fluxo (20/10)

O diagnóstico de 20/10 (Fase 9) mostrou que o Reinaldo usa **headset** e que o detector quase nunca
disparava com ele. Ele gravou mais 94 falas com o mic do headset (`--out gravacoes-headset`, partes
perto, variado e negativas). Três achados:

1. **O treino e o cliente calculavam features diferentes.** O `train.py` tirava os embeddings do
   clipe inteiro de uma vez (`AudioFeatures._get_embeddings`); o cliente calcula em fluxo, de 80 em
   80 ms. Na voz os dois dão o mesmo valor, mas o melspectrograma corta o piso em 80 dB abaixo do
   ponto mais forte do trecho: no clipe inteiro o silêncio vira 3,08, num bloco só de silêncio vira
   −8. O mic do headset tem fundo em −90 dBFS (quase zero digital), então as pausas caem nesse piso
   e o modelo se perdia; o do notebook (−71 dBFS) ficava acima dele e funcionava. Por isso a
   avaliação dizia 91–100% nas falas do headset e o cliente acertava ~45% (os 3/6 do diagnóstico).
   O `train.py` agora calcula em fluxo, como o cliente (`embed()`), soma um ruído de fundo mínimo
   (0,5–3 LSB) no lugar dos zeros e desloca as cópias aumentadas em 0–80 ms dentro do bloco.
   Os negativos do ACAV100M e a validação de 10,7 h continuam em features do clipe inteiro (vêm
   prontas do openWakeWord): os falsos/h da validação são aproximados.
2. **O gravador pega um degrau ao abrir o mic do headset.** Ele abre o microfone a cada fala; o do
   headset só entrega som ~0,84 s depois, com um degrau de corrente contínua (até −10 dBFS, some em
   ~2 s) e um estalo de ~60 ms. Descartar 0,5 s no gravador não bastou. No treino as falas do
   headset passam por um passa-altas de 60 Hz e os primeiros 0,42 s viram silêncio; 9 das 64 falas
   com o nome começaram antes do mic acordar ("gatha") e ficaram de fora dos positivos.
3. **O nível do headset é ~20 dB menor** (voz em −50 dBFS, contra −30 no notebook), o mesmo do uso
   real; as gravações novas entram no treino como estão.

Resultado no limiar do cliente (0,6), no caminho do cliente, só com falas que nenhum dos modelos
viu no treino (`bench/results/wake-baseline-20261020-200511.md` e `wake-train-20261020-200528.md`):

| | modelo de 12/10 | modelo de 20/10 (2 dobras) |
|---|---|---|
| headset, perto | 44% (34) | **100% (34)** |
| headset, variado | 48% (21) | **95% (21)** |
| negativas do headset (30) | 0 | 1 ("a gata subiu no telhado") |
| falsos/h na validação 10,7 h | 21,6 | 15,7 (modelo final: 17,9) |

Na rodada do notebook (avaliação honesta): perto 95%, variado 83%, longe 83%, eco com AEC 73%; as
negativas parecidas disparam 8/30 ("a gata", "agora não dá", "a batata", "Atacama"…), como em 12/10.
No barge-in um falso disparo só pausa a resposta até o servidor conferir a transcrição.

**Decisão:** o modelo de 20/10 substitui o de 12/10 em `/dados/agatha/models/wakeword/agatha.onnx`
(o anterior fica em `agatha-20261012.onnx`). Toda avaliação de detector daqui em diante usa o
caminho em fluxo; o autoteste do cliente (`python -m agatha_client.wakeword`) mede o mesmo.

**Teste no Windows (20/10, headset, `--barge-debug`, 9 falas):** 2 de 2 "Agatha, para" no meio da
resposta interromperam (disparo 485 ms depois do começo da fala, confirmado pelo servidor); nenhum
disparo à toa durante as respostas; CPU 2,2 ms por bloco de 32 ms. Amostra pequena: seguir medindo
no uso.

**Segundo teste (21/10, 12 falas):** 3 de 3 interrupções pararam a resposta ("para", "espera",
"chega"; disparo 515–578 ms depois do começo da fala); 1 disparo à toa (nota 0,76) num ruído de
~800 ms sem fala, que o servidor descartou pela transcrição vazia, e ela continuou. Somando os
dois testes: 5 de 5 interrupções, 1 disparo à toa. "Agatha, Paras" (o Whisper) e "Agatha, espera"
iam ao LLM como pergunta: entraram nas palavras de parar (`wake.STOP_WORDS`).

## Consequências

**Positivas**

- Caminho para interromper a Agatha sem depender do servidor nem da GPU.
- Nada muda na chamada normal, que já funciona (93% de acerto, 2,7 falsos/h).
- Treino 100% local e reproduzível no `agatha-srv`, com avaliação honesta (2 dobras).

**Negativas / riscos**

- **Com eco, o acerto cai para ~25%.** O barge-in depende do cancelamento de eco (próxima etapa);
  sem ele, não funciona com as caixas do notebook.
- **Poucos dados**: 82 falas de teste; cada ponto percentual é incerto (±5–10%).
- **Modelo pessoal**: treinado só com a voz do Reinaldo, no mic do notebook e no do headset (20/10).
  Outra pessoa ou outro microfone precisa de novas gravações.
- **Mais uma dependência no cliente** (`openwakeword`, `onnxruntime`) e ~3 MB de modelos.
- Para melhorar, o caminho é mais gravações reais (os sintéticos não ajudaram). Guardar o áudio
  das falas do dia a dia para retreinar exige autorização do Reinaldo, ainda não dada.
