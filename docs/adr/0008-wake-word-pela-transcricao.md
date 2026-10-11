# ADR-008: Wake word pela transcrição, com modo conversa

- **Status:** Aceito
- **Data:** 2026-10-11
- **Substitui parcialmente:** ADR-001, wake word com openWakeWord no cliente (passa a ser o caminho
  de evolução, não o ponto de partida)

## Contexto

Até a Fase 2, a Agatha respondia a **qualquer** fala captada: vídeos, música, conversas ao redor.
O half-duplex (ADR-006) evita que ela responda a si mesma, mas não resolve isso. O ADR-001 previa
uma wake word ("Agatha") detectada no cliente pelo openWakeWord. No levantamento da Fase 3:

- **não existe modelo pronto de "Agatha"**. Procurei no openWakeWord, numa coleção com 241 modelos
  da comunidade e no Hugging Face;
- um modelo próprio exige **treino com fala sintética**: dezenas de GB de áudio negativo, PyTorch,
  milhares de pronúncias de "Agatha". As vozes PT-BR disponíveis são poucas, o que limita a
  variedade das amostras;
- o servidor já transcreve toda fala detectada (VAD + STT em ~0,35 s), e "Agatha" já está no
  vocabulário do Whisper (ADR-007).

## Decisão

1. **A wake word é detectada na transcrição, no servidor** (`server/agatha_server/wake.py`). A
   Agatha só responde se o nome aparecer:
   - entre as **4 primeiras palavras** ("Agatha, que horas são?", "Certo, Agatha. ..."), ou
   - entre as **2 últimas** ("Ok, obrigado, Agatha").

   A comparação ignora acentos e maiúsculas e aceita variações de grafia (`agatha`, `agata`,
   `agatah`). Uma menção no meio de uma frase longa ("o livro da Agatha Christie...") não conta.

2. **Modo conversa:** depois de cada resposta, por **10 s** (`AGATHA_CONVERSATION_S`), falas sem o
   nome também são aceitas. Vale o momento em que a fala **começou**, então uma pergunta longa
   iniciada dentro da janela conta. Cada resposta renova a janela.

3. **Frase cortada por pausa (ADR-007):** a continuação de uma fala que chamou a Agatha também
   conta como dirigida a ela.

4. **Protocolo v2, com campos novos e opcionais:**
   - a mensagem `transcript` ganha `addressed`, que diz se a fala era para a Agatha;
   - o `ready` informa as palavras aceitas e a duração do modo conversa;
   - o cliente mostra as falas ignoradas de forma discreta.

5. **Configurável:** `AGATHA_REQUIRE_WAKE_WORD` (liga ou desliga), `AGATHA_WAKE_WORDS` e
   `AGATHA_CONVERSATION_S`.

6. **O openWakeWord continua sendo o caminho para aperfeiçoar** (decisão do Reinaldo). Um modelo
   próprio de "Agatha" no cliente entra quando a detecção pela transcrição mostrar limites, ou como
   evolução planejada: menos áudio enviado, privacidade e STT ocioso. Os dados de uso desta solução
   (acertos e disparos falsos nos logs) servem de base para esse treino.

## Consequências

**Positivas**

- Ela deixa de responder a vídeos, música e conversas ao redor, sem treinar nada.
- Nenhuma latência extra: o nome e a pergunta chegam na mesma frase.
- O modo conversa mantém o diálogo natural, sem repetir o nome a cada pergunta.
- Os logs registram cada fala ignorada (`ignorada: não chamou a Agatha`), o que permite medir
  disparos falsos e falas perdidas.

**Negativas / riscos**

- **O STT continua rodando em toda fala captada**, mesmo nas ignoradas: ~0,35 s de GPU cada, e
  todo o áudio vai do PC para o servidor (dentro da LAN). Com TV ligada perto do microfone, a GPU
  trabalha à toa. A wake word no cliente (openWakeWord) resolveria isso.
- **Depende do STT acertar o nome.** Se "Agatha" for mal transcrita, a fala é ignorada. O
  vocabulário do Whisper ajuda, e na Fase 2 o nome passou a ser reconhecido de forma consistente.
- **O vocabulário pode favorecer "Agatha" demais:** o `initial_prompt` pode levar o Whisper a
  "ouvir" o nome em sons parecidos. Precisa ser acompanhado nos logs.
- **O modo conversa aceita qualquer fala por 10 s**, inclusive uma conversa paralela logo depois
  de uma resposta.
