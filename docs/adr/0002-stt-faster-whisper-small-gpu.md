# ADR-002: STT com faster-whisper `small` na GPU

- **Status:** Aceito
- **Data:** 2026-10-11
- **Substitui parcialmente:** ADR-001, item 5 ("STT na CPU") e a linha STT da tabela de componentes

## Contexto

O ADR-001 previa o STT na CPU, para deixar os 4 GB de VRAM da GTX 1050 Ti livres para o LLM, e
estabelecia a regra "medir antes de mudar". A meta do projeto é ficar abaixo de 2 s entre o fim da
fala e o início da resposta, e o STT é só a primeira parte desse caminho. Depois dele vêm o primeiro
token do LLM e o primeiro trecho de áudio do TTS.

Como o Whisper não faz streaming de verdade, o pipeline segmenta a fala com o VAD (Silero) e
transcreve cada frase inteira. A latência do STT é, portanto, a soma de dois tempos:

1. o silêncio que o VAD espera para considerar a frase encerrada (cerca de 0,5 s, a ajustar);
2. o tempo de transcrição do segmento.

O encoder do Whisper sempre processa uma janela de 30 s. Por isso, um comando de voz curto não
fica proporcionalmente mais rápido de transcrever que uma frase longa.

### Medições

Benchmark em `bench/stt_bench.py`, com 30 frases do FLEURS pt_br (split dev, seed 42), quantização
int8 e beam 1. A coluna "comando 3 s" mede recortes de 3 s, o caso típico de um comando de voz.
Resultados brutos: `bench/results/stt-20261011-1546.json` (CPU) e
`bench/results/stt-cuda-20261011-1555.json` (GPU).

| modelo         | WER   | comando 3 s, CPU (i5-7400) | comando 3 s, GPU (1050 Ti) | VRAM    |
|----------------|-------|----------------------------|----------------------------|---------|
| tiny           | 26,2% | 0,27 s                     | 0,07 s                     | 122 MB  |
| base           | 13,7% | 0,49 s                     | 0,12 s                     | 186 MB  |
| small          | 7,1%  | 1,39 s                     | 0,34 s                     | 378 MB  |
| medium         | 4,7%  | 4,05 s                     | 0,92 s                     | 1018 MB |
| large-v3-turbo | 3,7%  | 7,43 s                     | 1,31 s                     | 1082 MB |

Os valores são medianas. O WER veio da rodada na GPU; na CPU os números são equivalentes (diferença
de até 2 pontos, maior nos modelos pequenos). A VRAM inclui o contexto CUDA do processo.

Variações do `small` na GPU (`bench/results/stt-cuda-20261011-1556.json`, `-1557.json` e `-155836.json`):

| variação       | WER  | comando 3 s | VRAM    |
|----------------|------|-------------|---------|
| int8, beam 1   | 7,1% | 0,34 s      | 378 MB  |
| int8_float32   | 7,1% | 0,34 s      | 378 MB  |
| float32        | 7,1% | 0,47 s      | 1178 MB |
| int8, beam 5   | 6,8% | 0,36 s      | 378 MB  |

O que as medições mostram:

- **Na CPU nenhum modelo atende.** Com o `base` a latência cabe (cerca de 1 s somando VAD e
  transcrição), mas ele erra 14–15% das palavras em fala lida, sem jargão. O `small` levaria cerca
  de 1,9 s só no STT, consumindo quase todo o orçamento.
- **Na GPU tudo fica de 3 a 6 vezes mais rápido**, e o `small` usa menos de 0,4 GB de VRAM.
- O `large-v3-turbo` é mais lento que o `medium` nas duas plataformas, porque o encoder de 32
  camadas domina o custo, e ganha só 1 ponto de WER.
- Em Pascal (compute capability 6.1) o CTranslate2 não oferece float16. O `int8` é o mais rápido e
  o mais econômico. O beam 5 ganha 0,3 ponto de WER, cerca de 2 palavras em 30 frases, o que está
  dentro do ruído da amostra.

## Decisão

1. **STT com faster-whisper, modelo `small`, na GPU**, com a configuração:
   - `device="cuda"`, `compute_type="int8"`, `beam_size=1`;
   - idioma fixo `language="pt"`, sem detecção automática;
   - `without_timestamps=True` e `condition_on_previous_text=False`, porque cada frase é transcrita
     de forma independente;
   - VAD fora do Whisper (`vad_filter=False`). A segmentação é feita pelo Silero VAD no pipeline.

2. **Orçamento de VRAM revisado:**

   | componente               | VRAM          |
   |--------------------------|---------------|
   | STT (`small`, int8)      | ~0,4 GB       |
   | LLM (3–4B quantizado)    | até ~3,4 GB   |
   | margem                   | ~0,2 GB       |

3. **Bibliotecas CUDA:** cuBLAS 12 e cuDNN 9. Durante o desenvolvimento elas vêm dos wheels da
   NVIDIA no PyPI (`bench/requirements-gpu.txt`), dentro do venv. Nos containers, vêm da imagem
   `nvidia/cuda:12.x` com cuDNN ou dos mesmos wheels; a escolha fica para quando o container for
   criado. O host continua só com o driver (branch 580). Versões validadas na GTX 1050 Ti:

   | pacote         | versão     |
   |----------------|------------|
   | faster-whisper | 1.2.1      |
   | ctranslate2    | 4.8.2      |
   | cuBLAS         | 12.9.2.10  |
   | cuDNN          | 9.27.0.42  |

4. **Áudio de entrada como PCM 16 kHz mono**, passado ao modelo como array numpy. O
   `decode_audio` do faster-whisper 1.2.1 é incompatível com o PyAV 19 e não deve ser usado. O
   servidor recebe áudio bruto pelo WebSocket, então ele não é necessário.

5. **Plano B:** se o `small` errar demais com o vocabulário real (Zabbix, Oracle, nomes de hosts),
   a primeira alternativa é tentar um prompt inicial ou hotwords com esses termos. Se não bastar,
   trocar para o `medium` na GPU (4,7% de WER, 0,92 s, ~1 GB de VRAM), aceitando menos VRAM para o
   LLM e uma latência mais apertada.

## Consequências

**Positivas**

- O STT fecha em cerca de 0,85 s (0,5 s de VAD + 0,34 s de transcrição), deixando ~1,1 s para o
  primeiro token do LLM e o primeiro áudio do TTS.
- Metade dos erros do `base` (7,1% contra 13,7%), com custo pequeno de VRAM.
- A CPU fica livre para VAD, TTS, núcleo e ferramentas.

**Negativas / riscos**

- **STT e LLM dividem a GPU.** Não foi medido ainda o comportamento com os dois carregados ao mesmo
  tempo, nem a disputa quando houver sobreposição, por exemplo ao transcrever a próxima fala
  enquanto o LLM ainda gera. Isso será medido na Fase 2.
- **A margem de VRAM para o LLM encolhe** de ~4 GB para ~3,4 GB, o que pode limitar o tamanho do
  modelo ou do contexto.
- **Mais uma dependência presa ao Pascal.** Versões futuras do cuDNN ou do CTranslate2 podem
  abandonar a compute capability 6.1. As versões acima ficam fixadas e só são atualizadas depois
  de um teste na GPU.
- **O WER do FLEURS não representa o uso real.** É fala lida da Wikipédia, sem jargão técnico. A
  escolha precisa ser validada com gravações do próprio usuário antes de ser considerada definitiva.
- Em desenvolvimento, o `LD_LIBRARY_PATH` precisa apontar para as bibliotecas CUDA do venv.
