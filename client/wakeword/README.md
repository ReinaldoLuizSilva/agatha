# client/wakeword/

Treino do detector da wake word "Agatha" que roda no cliente (Fase 5). Não existe modelo pronto
de "Agatha" (ADR-008), então ele é treinado aqui, no `agatha-srv`, **no CPU** e sem parar a
Agatha. O modelo final é pequeno (um classificador sobre os embeddings do
[openWakeWord](https://github.com/dscripka/openWakeWord)) e vai para
`/dados/agatha/models/wakeword/agatha.onnx`, fora do git.

## Etapas

| etapa | script | o que faz | tempo |
|---|---|---|---|
| 1 | `python -m agatha_client.record` (no notebook) | grava a voz do Reinaldo: o nome, frases sem o nome, eco e ambiente | ~30 min |
| 2 | `generate.py` | sintetiza "Agatha" nas vozes PT-BR do Piper (cadu, edresson, faber, jeff) e frases parecidas, mais as respostas reais na voz da Agatha | ~20 min |
| 3 | `train.py --eval` | avalia em 2 dobras, sem testar com falas vistas no treino | ~1 h |
| 4 | `train.py --final` | treina com tudo e exporta o ONNX | ~30 min |

A voz da própria Agatha ("dii") fica só nos negativos: o detector não pode interromper a fala
dela se ela disser o próprio nome pelas caixas.

## Ambiente

```bash
python3 -m venv .venv-wake
.venv-wake/bin/pip install -r bench/requirements-wake.txt
.venv-wake/bin/pip install --no-deps openwakeword==0.6.0
.venv-wake/bin/pip install piper-tts==1.8.0 onnx==1.23.2
.venv-wake/bin/pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
```

Dados em `/dados/agatha/data/wakeword/`:

| pasta | conteúdo |
|---|---|
| `gravacoes/` | gravações do Reinaldo (`manifest.jsonl`: vale a última linha de cada id) |
| `sintetico/` | saída do `generate.py` |
| `oww/` | 2.000 h de áudio variado já em embeddings (ACAV100M, 17,3 GB) e 10,7 h de validação, do [openWakeWord](https://huggingface.co/datasets/davidscripka/openwakeword_features) |
| `features/` | cache dos embeddings calculados pelo `train.py` (apague para recalcular) |

## Uso

```bash
cap="systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 nice -n 19"
$cap .venv-wake/bin/python client/wakeword/generate.py --out /dados/agatha/data/wakeword/sintetico
$cap .venv-wake/bin/python client/wakeword/train.py --eval     # tabela em bench/results/wake-train-*.md
$cap .venv-wake/bin/python client/wakeword/train.py --final    # → /dados/agatha/models/wakeword/agatha.onnx
```

A Agatha continua no ar durante o treino, então ele roda com a menor prioridade de CPU
(`nice -n 19`) e com **teto de 5 GB de memória**. Sem o teto, um teste que carregou 6 GB de uma vez
jogou o LLM e o servidor no swap, e o primeiro áudio subiu de ~2 s para 34 s até as páginas
voltarem. Com o teto, quem morre é o treino. A comparação com os outros detectores está em
`bench/wake_bench.py`.

O que a avaliação ensinou:

- **A voz do Reinaldo é o que funciona.** Os sintéticos do Piper dobraram os falsos disparos sem
  melhorar o acerto, então ficam desligados (`--synthetic 0`, o padrão).
- **Silêncio antes de cada gravação.** A 1ª janela completa só termina em ~1,96 s e o nome
  costuma terminar antes; sem os 1,5 s de silêncio não havia janela no fim do nome.
- **Peso alto nos negativos (até 100) e mineração de negativos difíceis** levaram os falsos
  disparos na validação de 10–20/h para ~1–3/h.
