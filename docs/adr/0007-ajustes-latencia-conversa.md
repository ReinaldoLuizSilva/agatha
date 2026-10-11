# ADR-007: Ajustes de latência e de conversa da Fase 2

- **Status:** Aceito
- **Data:** 2026-10-11
- **Complementa:** [ADR-004](0004-llm-gemma-4-e4b.md) (LLM e prompt), [ADR-005](0005-tts-piper-dii.md) (TTS),
  [ADR-006](0006-protocolo-v2-half-duplex.md) (protocolo v2)

## Contexto

Com o pipeline completo no ar (STT → LLM → TTS, protocolo v2), a primeira conversa medida ficou
bem acima da estimativa do ADR-004 (~1,7 s). Foram 5 perguntas faladas, com um cliente simulado
(`server/scripts/send_wav.py`):

- **2,58 s (p50)** e **3,59 s (p90)** entre o fim da fala e o primeiro áudio;
- respostas com a hora errada;
- "Agatha" transcrita como "Álvaro";
- explicações inventadas para perguntas mal transcritas.

Os logs do llama-server e os tempos por etapa (`response_end.timings`) mostraram que as causas
não eram os componentes em si, mas a forma como eles estavam combinados. A primeira sessão real no
Windows também revelou dois pontos:

- picos de até 3,78 s depois de alguns turnos de conversa;
- frases cortadas por pausas, que, com o half-duplex, faziam a segunda metade ser descartada.

## Decisão

Cada ajuste abaixo foi medido antes de ser mantido. As medições estão em
`bench/results/e2e-conversa-*.json`.

### Cache do prompt no llama.cpp

O Gemma 4 usa *sliding window attention*. Com ela, **qualquer mudança no começo do prompt obriga o
llama.cpp a reprocessar o prompt inteiro** (~230 tokens/s nesta GPU, ou seja, 1,5 a 2 s).

1. **Data e hora na mensagem do usuário**, no formato `[domingo, 11/10/2026, 18h22]`, e não no
   prompt de sistema. Antes, a virada do minuto invalidava o cache.
2. **O histórico guarda exatamente o que foi enviado e gerado** (o texto bruto do modelo), e não o
   texto limpo para a fala.
3. **Histórico cortado em bloco:** até 12 trocas. Ao passar disso, volta para as 6 mais recentes
   de uma vez. Cortar uma troca por turno mudava o prefixo a cada turno (medido: 525 tokens em 2,1 s
   na 8ª pergunta).
4. **Pré-aquecimento do cache:**
   - depois de um corte do histórico, o prompt cortado é enviado ao llama-server (`max_tokens=1`)
     em segundo plano, enquanto a resposta ainda está sendo falada;
   - na partida do servidor, o mesmo é feito com o prompt de sistema.

### CPU compartilhada entre LLM e TTS

5. **llama-server com 2 threads** (`-t 2`): todas as camadas estão na GPU, e mais threads só
   disputam a CPU.
6. **Piper com 3 threads ONNX** (`AGATHA_TTS_THREADS`). Medido com o LLM gerando em paralelo:
   3 threads levam ~116 ms por frase, e o padrão (4) levava ~224 ms.

### Texto e prompt (prompt v3, em `server/agatha_server/prompts.py`)

7. **Horas, datas e números sempre em algarismos.** Quando escrevia a hora por extenso, o modelo
   errava ("dezessete horas" às 18h). O núcleo converte para a fala, incluindo datas `dd/mm/aaaa`.
   Os algarismos também encurtam a primeira frase.
8. **Primeira frase de no máximo seis palavras**, e um pedido para repetir quando a pergunta
   parecer mal transcrita.
9. **Primeiro trecho cortado mais cedo:** na primeira vírgula depois de 15 caracteres (antes, 30),
   por exemplo "Para rodar o Zabbix,".

### Reconhecimento de fala

10. **Vocabulário no `initial_prompt` do Whisper**, que era o plano B do ADR-002: "Agatha, Zabbix,
    Oracle, trigger, tablespace, Linux, Podman". "Agatha" deixou de virar "Álvaro", sem custo
    mensurável no tempo de transcrição.

### Fim da fala (endpointing)

11. **Frase que parece cortada é segurada:** se a transcrição termina em reticências, vírgula ou
    numa palavra como "e", "de", "que" ou "para", o servidor espera até 700 ms
    (`AGATHA_VAD_CONTINUATION_MS`) por uma continuação e junta as partes. Frases completas não
    esperam nada. Isso evitou aumentar o silêncio do VAD para todas as respostas, que custaria
    +300 ms sempre.

## Consequências

**Resultado medido** (fim da fala → primeiro áudio, cliente simulado no próprio servidor):

| rodada | mudança principal | p50 | p90 | pior |
|---|---|---|---|---|
| 1 | pipeline completo, sem ajustes | 2,58 s | 3,59 s | 3,59 s |
| 2 | cache do prompt (1, 2), prompt v3, `-t 2` | 2,26 s | 2,62 s | 2,62 s |
| 3 | Piper com 3 threads | 2,11 s | 2,32 s | 2,32 s |
| 4 | vocabulário no STT, algarismos obrigatórios | 1,94 s | 2,17 s | 2,17 s |
| 5 | 14 perguntas: corte em bloco + pré-aquecimento | 1,86 s | 2,16 s | 2,31 s |
| 6 | **em container (pod), pela LAN, com aquecimento na partida** | **1,84 s** | **2,09 s** | **2,17 s** |

Na sessão real do Windows, a rede e a reprodução somaram **~130 ms**, sendo 91 ms de latência de
saída da placa. A latência sentida fica, portanto, em torno de **2,0 s de mediana**.

**Positivas**

- A meta de menos de 2 s é atingida na mediana do lado do servidor, e o pior caso caiu de 3,8 s
  para ~2,2 s.
- Hora e contas corretas, porque o modelo copia algarismos e o núcleo faz a conversão para a fala.
- Pausas no meio da frase não fazem mais a Agatha responder pela metade.

**Negativas / limites**

- **O que sobra é estrutural:**
  - VAD + STT levam ~0,87 s (0,5 s de silêncio + 0,35 s de transcrição);
  - o tamanho da primeira frase depende do modelo, que nem sempre respeita as "seis palavras";
  - descer mais exigiria trocar latência por qualidade, como menos silêncio no VAD ou um STT menor.
- **Perguntas mal transcritas ainda geram respostas inventadas:** o modelo de 4B raramente percebe
  que a pergunta não faz sentido, mesmo com instrução, temperatura menor ou exemplos (medido:
  0 a 2 em 8 tentativas). O remédio é errar menos na transcrição e, na Fase 3, usar ferramentas.
- **Frases segurando a resposta:** uma frase completa que termina numa das palavras da lista
  ("... o que é isso que") espera 700 ms à toa.
- **Mais estado e mais acoplamento com o comportamento do llama.cpp:** o cache depende de o prefixo
  do prompt não mudar. Qualquer mudança no histórico ou no prompt precisa preservar isso, e os
  testes do núcleo verificam.
