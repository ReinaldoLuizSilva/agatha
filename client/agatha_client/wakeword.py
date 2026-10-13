"""Detector da wake word "Agatha" no cliente, só para o barge-in (ADR-014).

openWakeWord: melspectrograma e embedding pré-treinados + o classificador treinado com a voz do
Reinaldo (`client/wakeword/train.py`). Os três ONNX ficam numa pasta fora do git, copiada de
/dados/agatha/models/wakeword no agatha-srv (veja o README do cliente).
"""

from pathlib import Path

import numpy as np

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
            best = max(best, float(next(iter(self._model.predict(chunk).values()))))
        return best
