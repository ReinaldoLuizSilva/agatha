"""Detector da wake word "Agatha" no cliente, só para o barge-in (ADR-014).

openWakeWord: melspectrograma e embedding pré-treinados + o classificador treinado com a voz do
Reinaldo (`client/wakeword/train.py`). Os três ONNX ficam numa pasta fora do git, copiada de
/dados/agatha/models/wakeword no agatha-srv (veja o README do cliente).
"""

import logging
import math
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_FILES = ("agatha.onnx", "melspectrogram.onnx", "embedding_model.onnx")
CHUNK = 1280  # 80 ms: o openWakeWord calcula um embedding a cada bloco desse tamanho


class WakeDetector:
    def __init__(self, model_dir: str | Path):
        model_dir = Path(model_dir)
        missing = [f for f in MODEL_FILES if not (model_dir / f).is_file()]
        if missing:
            raise FileNotFoundError(f"faltam em {model_dir}: {', '.join(missing)}")
        from openwakeword.model import Model  # ImportError se o extra [barge-in] não foi instalado

        self._model = Model(
            wakeword_models=[str(model_dir / "agatha.onnx")], inference_framework="onnx",
            melspec_model_path=str(model_dir / "melspectrogram.onnx"),
            embedding_model_path=str(model_dir / "embedding_model.onnx"),
        )
        self._buf = np.zeros(0, np.int16)
        self.nan_count = 0  # notas inválidas: viravam 0,00 em silêncio (teste de 20/10)

    def reset(self) -> None:
        self._model.reset()
        self._buf = np.zeros(0, np.int16)

    def feed(self, pcm: np.ndarray) -> float:
        """Recebe áudio de 16 kHz (int16) em pedaços de qualquer tamanho; devolve a maior nota dos
        blocos de 80 ms completados agora (0 se nenhum completou)."""
        self._buf = np.concatenate([self._buf, pcm.astype(np.int16, copy=False)])
        best = 0.0
        while len(self._buf) >= CHUNK:
            chunk, self._buf = self._buf[:CHUNK], self._buf[CHUNK:]
            score = float(next(iter(self._model.predict(chunk).values())))
            if math.isnan(score):
                self.nan_count += 1
                if self.nan_count in (1, 100, 1000):
                    log.warning("o detector devolveu uma nota inválida (NaN) %d vez(es)", self.nan_count)
                    print(f"  (aviso: o detector devolveu nota inválida (NaN) {self.nan_count} vez(es))", flush=True)
                continue
            best = max(best, score)
        return best


def versions() -> str:
    """Versões que importam para o detector (diagnóstico)."""
    import onnxruntime
    from importlib.metadata import version

    return (f"Python {sys.version.split()[0]}, numpy {np.__version__}, onnxruntime {onnxruntime.__version__}, "
            f"openwakeword {version('openwakeword')}")


def main() -> int:
    """Autoteste: `python -m agatha_client.wakeword gravacoes\\perto\\perto-012.wav [--model models]`.
    Passa a gravação pelo detector em blocos de 512 amostras, como no cliente, e mostra a maior nota."""
    import argparse
    import wave

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("wav", nargs="+")
    parser.add_argument("--model", default="models")
    args = parser.parse_args()
    print(versions())
    det = WakeDetector(args.model)
    for path in args.wav:
        with wave.open(path) as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1:
                print(f"{path}: precisa ser 16 kHz mono")
                continue
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        det.reset()
        pcm = np.concatenate([np.zeros(16000, np.int16), pcm, np.zeros(8000, np.int16)])  # 1 s antes, como no treino
        best = max((det.feed(pcm[i:i + 512]) for i in range(0, len(pcm) - 511, 512)), default=0.0)
        print(f"{path}: nota máxima {best:.2f}" + (f" · {det.nan_count} notas NaN" if det.nan_count else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
