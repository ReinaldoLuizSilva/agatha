# ADR-005: TTS com Piper e a voz feminina "dii" (PT-BR), na CPU

- **Status:** Aceito (provisório quanto à voz: a ideia é trocar por uma voz mais limpa quando houver)
- **Data:** 2026-10-11

## Contexto

O ADR-001 definiu o Piper como TTS. Faltava escolher a voz. O Reinaldo prefere uma voz
**feminina** em PT-BR.

O orçamento de latência (ADR-004) reserva cerca de **0,15 s** para o TTS gerar o primeiro áudio. A
GPU não tem espaço: o pico já é de 3,66 GB de 4 GB, com STT e LLM carregados. Por isso o TTS roda
na **CPU**, que também atende o VAD e o núcleo.

Como o pipeline envia o texto ao TTS em trechos (ADR-004), o que importa é o tempo até o
**primeiro áudio da primeira frase curta**. Depois disso, basta o TTS sintetizar mais rápido que
o tempo real (RTF < 1) para a fala não ter buracos.

### Opções de voz

- **Piper oficial** (`rhasspy/piper-voices`): só tem vozes **masculinas** em PT-BR (cadu, edresson,
  faber, jeff).
- **Piper, comunidade**: a voz **dii** (`OpenVoiceOS/pipertts_pt-BR_dii`) é feminina e PT-BR, mas
  foi ajustada a partir de uma voz de português de Portugal com dados sintéticos. Licença
  CC BY-NC-ND 4.0.
- **Kokoro v1.0** (82M parâmetros): tem a voz feminina PT-BR **pf_dora**, com licença Apache 2.0,
  rodando na CPU pelo `kokoro-onnx` (MIT).

### Medições

Feitas com `bench/tts_bench.py` no i5-7400, com 4 núcleos, em 7 frases no estilo do prompt v2.
Resultados em `bench/results/tts-20261011-175059.json`; as amostras WAV ficaram fora do git.

| voz                     | gênero    | 1º áudio (frase curta) | RTF p50 |
|-------------------------|-----------|------------------------|---------|
| **Piper dii**           | feminina  | **34 ms**              | 0,039   |
| Kokoro pf_dora (fp32)   | feminina  | 686 ms                 | 0,376   |
| Kokoro pf_dora (int8)   | feminina  | 660 ms                 | 0,394   |
| Piper faber (referência) | masculina | 30 ms                 | 0,040   |

- O Piper é cerca de **10 vezes mais rápido** e deixa a CPU quase livre.
- O Kokoro somaria uns **0,5 s** à primeira frase, levando a estimativa total para ~2,2 s, acima
  da meta. Também ocuparia os 4 núcleos durante a síntese. A versão int8 não ajuda nesta CPU, que
  não tem as instruções de aceleração int8 (VNNI).
- Na avaliação de ouvido, o Reinaldo considerou a dii **boa o suficiente por enquanto**, mas
  gostaria de uma voz mais limpa e agradável no futuro.

## Decisão

1. **TTS: Piper na CPU**, com o pacote `piper-tts` 1.8.0. É o projeto `OHF-Voice/piper1-gpl`, a
   continuação ativa do `rhasspy/piper`, que foi arquivado.

2. **Voz: dii PT-BR** (`OpenVoiceOS/pipertts_pt-BR_dii`), com um único locutor, 22 050 Hz e
   fonética do eSpeak `pt-br`:

   | arquivo               | tamanho          | SHA-256                                                            |
   |-----------------------|------------------|--------------------------------------------------------------------|
   | `dii_pt-BR.onnx`      | 63 511 038 bytes | `fb15a447ba7482b922e1d2fefea71c1031f760e72be24cce7794e26fe11d086b` |
   | `dii_pt-BR.onnx.json` |                  | `306998295ac96a449d43a19c8aad65b31e040341e15decbdf4aa68d88670ec84` |

   Os arquivos ficam em `/dados/agatha/models/tts/piper/`, fora do git.

3. **Normalização do texto no núcleo, antes do TTS:** números, porcentagens, IPs, versões e siglas
   são convertidos em palavras pelo próprio servidor. A amostra "texto cru" (`192.168.1.25`,
   `85%`, `Podman 5.8`) mostra que não dá para depender do eSpeak para isso. Essa etapa se soma à
   limpeza de markdown e símbolos prevista no ADR-004.

4. **Áudio de saída a 22 050 Hz, mono**, a taxa nativa da voz. O protocolo v2 vai declarar a taxa
   do áudio de resposta, em vez de assumir 16 kHz como na entrada, e o cliente reproduz nessa taxa.

## Consequências

**Positivas**

- O TTS praticamente sai do orçamento de latência (~35 ms para o primeiro áudio). A estimativa
  total continua em ~1,7 s (p50).
- A CPU fica quase livre durante a fala, sem disputa com o VAD e o núcleo.
- A voz é feminina, como preferido.

**Negativas / riscos**

- **Qualidade da voz:** a dii é aceitável, mas não é a voz final desejada. Ela foi treinada com
  dados sintéticos a partir de uma voz portuguesa.
- **Licença da voz CC BY-NC-ND 4.0:** permite o uso pessoal e não comercial sem modificar o modelo.
  Se a Agatha um dia for distribuída ou usada comercialmente, a voz terá de ser trocada.
- **`piper-tts` é GPL-3.0:** se o servidor importar o Piper como biblioteca e o código for
  distribuído, a obra combinada fica sujeita à GPL. Isso deve ser considerado na escolha da
  licença do repositório. A alternativa é isolar o TTS num processo ou container separado.
- **Fonética via eSpeak:** pronúncia de termos em inglês (trigger, tablespace, Podman) e de siglas
  pode sair estranha. A normalização do núcleo pode mitigar, com um dicionário de pronúncias.

**Quando revisitar (troca de voz)**

- Surgir uma voz feminina PT-BR para o Piper, oficial ou da comunidade, com melhor qualidade.
- Sobrar VRAM ou CPU para um TTS neural mais pesado. O **Kokoro pf_dora** é a alternativa já
  medida: precisaria de ~0,5 s a mais por primeira frase na CPU, ou de espaço na GPU.
- Para trocar de voz, basta mudar o modelo e rodar de novo o `bench/tts_bench.py`. Se a taxa de
  áudio for diferente, o protocolo já a declara.
