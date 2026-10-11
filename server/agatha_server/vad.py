"""Detecção de fala em streaming e segmentação em frases.

- `SileroVAD`: roda o Silero VAD (ONNX embutido no faster-whisper) bloco a bloco, mantendo o
  estado da rede entre chamadas.
- `Segmenter`: máquina de estados que transforma probabilidades de fala em frases completas,
  emitindo `SpeechStart` e `SpeechEnd` (ver docs/protocol.md, evento `vad`).
"""

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # bloco nativo do Silero a 16 kHz
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE  # 32 ms
CONTEXT_SAMPLES = 64  # o Silero v5+ recebe as últimas 64 amostras do bloco anterior


class SileroVAD:
    """Silero VAD em streaming: recebe um bloco de 512 amostras e devolve P(fala)."""

    def __init__(self, model_path: str | None = None):
        import onnxruntime
        from faster_whisper.utils import get_assets_path

        path = model_path or os.path.join(get_assets_path(), "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"esperado bloco de {FRAME_SAMPLES} amostras, veio {frame.shape}")
        x = np.concatenate([self._context, frame])[np.newaxis, :]
        prob, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = frame[-CONTEXT_SAMPLES:]
        return float(prob[0])


@dataclass(frozen=True)
class VADConfig:
    threshold: float = 0.5
    min_silence_ms: int = 500
    pre_roll_ms: int = 300
    min_speech_ms: int = 250
    max_segment_s: float = 25.0

    @property
    def neg_threshold(self) -> float:
        """Limiar para sair da fala (histerese recomendada pelo Silero)."""
        return max(self.threshold - 0.15, 0.01)


@dataclass(frozen=True)
class SpeechStart:
    segment: int
    audio_ms: int  # início real da fala no fluxo da sessão


@dataclass(frozen=True)
class SpeechEnd:
    segment: int
    audio_ms: int  # onde o fim foi detectado
    speech_end_ms: int  # fim do último bloco com fala ("fim da fala" da medição de ponta a ponta)
    reason: str  # "silence" | "max_length"
    audio: np.ndarray  # float32, pre-roll + fala + silêncio final
    speech_ms: int
    last_speech_at: float  # relógio monotônico ao processar o último bloco com fala
    ended_at: float  # relógio monotônico ao detectar o fim

    @property
    def endpoint_ms(self) -> float:
        return (self.ended_at - self.last_speech_at) * 1000


class Segmenter:
    """Agrupa blocos de áudio em frases a partir das probabilidades do VAD.

    Aceita áudio em pedaços de qualquer tamanho; internamente processa blocos de 512 amostras.
    Uma frase só é confirmada (e `SpeechStart` emitido) após `min_speech_ms` de fala; trechos
    mais curtos são descartados em silêncio.

    As posições (`audio_ms`) contam todas as amostras recebidas na sessão, inclusive as
    descartadas com `discard`/`reset` durante uma resposta (half-duplex, ADR-006).
    """

    def __init__(
        self,
        config: VADConfig,
        vad: Callable[[np.ndarray], float],
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._vad = vad
        self._clock = clock
        self._pending = np.zeros(0, dtype=np.float32)
        self._frame_index = 0
        self._offset_samples = 0  # amostras descartadas, que contam na posição mas não passam pelo VAD
        self._segment_count = 0
        self._pre_roll: deque[np.ndarray] = deque(maxlen=config.pre_roll_ms // FRAME_MS)
        self._silence_limit = -(-config.min_silence_ms // FRAME_MS)  # arredonda para cima
        self._max_frames = int(config.max_segment_s * 1000) // FRAME_MS
        self._in_speech = False

    def discard(self, n_samples: int) -> None:
        """Conta amostras recebidas que não serão analisadas (microfone ignorado)."""
        self._offset_samples += n_samples

    def reset(self) -> None:
        """Descarta a frase em andamento e o estado do VAD, mantendo a contagem de posição."""
        self._offset_samples += len(self._pending)
        self._pending = np.zeros(0, dtype=np.float32)
        self._in_speech = False
        self._frames = []
        self._pre_roll.clear()
        if hasattr(self._vad, "reset"):
            self._vad.reset()

    def _ms(self, frame_index: int) -> int:
        return (self._offset_samples + frame_index * FRAME_SAMPLES) * 1000 // SAMPLE_RATE

    def feed(self, samples: np.ndarray) -> list[SpeechStart | SpeechEnd]:
        """Processa áudio float32 mono 16 kHz e devolve os eventos gerados."""
        data = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        usable = len(data) - len(data) % FRAME_SAMPLES
        self._pending = data[usable:]
        events: list[SpeechStart | SpeechEnd] = []
        for start in range(0, usable, FRAME_SAMPLES):
            event = self._process_frame(data[start : start + FRAME_SAMPLES])
            if event is not None:
                events.append(event)
        return events

    def _process_frame(self, frame: np.ndarray) -> SpeechStart | SpeechEnd | None:
        prob = self._vad(frame)
        index = self._frame_index
        self._frame_index += 1
        now = self._clock()

        if not self._in_speech:
            if prob >= self.config.threshold:
                self._open(frame, index, now)
            else:
                self._pre_roll.append(frame)
            return None

        self._frames.append(frame)
        if prob >= self.config.neg_threshold:
            self._silence = 0
            self._last_speech_index = index
            self._last_speech_at = now
        else:
            self._silence += 1

        speech_frames = self._last_speech_index - self._start_index + 1
        if not self._confirmed and speech_frames * FRAME_MS >= self.config.min_speech_ms:
            self._confirmed = True
            self._segment_count += 1
            return SpeechStart(self._segment_count, self._ms(self._start_index))

        if self._silence >= self._silence_limit:
            return self._close("silence", index, now)
        if self._confirmed and index - self._start_index + 1 >= self._max_frames:
            return self._close("max_length", index, now)
        return None

    def _open(self, frame: np.ndarray, index: int, now: float) -> None:
        self._in_speech = True
        self._confirmed = False
        self._frames = list(self._pre_roll) + [frame]
        self._pre_roll.clear()
        self._start_index = index
        self._last_speech_index = index
        self._last_speech_at = now
        self._silence = 0

    def _close(self, reason: str, index: int, now: float) -> SpeechEnd | None:
        self._in_speech = False
        frames, self._frames = self._frames, []
        if not self._confirmed:
            return None
        return SpeechEnd(
            segment=self._segment_count,
            audio_ms=self._ms(index + 1),
            speech_end_ms=self._ms(self._last_speech_index + 1),
            reason=reason,
            audio=np.concatenate(frames),
            speech_ms=(self._last_speech_index - self._start_index + 1) * FRAME_MS,
            last_speech_at=self._last_speech_at,
            ended_at=now,
        )
