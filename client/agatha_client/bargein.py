"""Barge-in no cliente (ADR-016): ouvir "Agatha" por cima da própria voz dela.

Cada bloco do stream duplex chega com o microfone e o que foi tocado no mesmo instante. Aqui:

1. o atraso do eco é medido pela própria fala da Agatha, nos primeiros segundos em que ela fala
   na sessão (`estimate_delay`), sem bipe; com fone de ouvido (sem eco), desiste e usa 0;
2. o cancelamento de eco linear (ADR-015) limpa o microfone; o áudio limpo é o que vai ao servidor;
3. o detector (ADR-014) ouve o áudio limpo o tempo todo, mas só dispara quando `active` (ela
   respondendo) e com o atraso já medido;
4. os últimos `pre_roll_s` ficam guardados: num disparo, o servidor recebe o áudio desde antes do
   nome, para transcrever a frase inteira ("Agatha, para") e confirmar.
"""

from collections import deque
from collections.abc import Callable
from typing import Protocol

import numpy as np

from .aec import EchoCanceller, estimate_delay

REF_ACTIVE = 300  # amplitude (int16) a partir da qual a referência conta como "ela falando"


class SupportsFeed(Protocol):
    def feed(self, pcm: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class BargeIn:
    def __init__(self, detector: SupportsFeed, sample_rate: int = 16000, threshold: float = 0.9,
                 pre_roll_s: float = 1.0, delay_ms: float | None = None, refractory_s: float = 1.5,
                 measure_s: float = 2.0, min_confidence: float = 10.0, max_attempts: int = 3,
                 frame_samples: int = 512, on_delay: Callable[[float, float | None], None] | None = None):
        self.detector = detector
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.refractory_s = refractory_s
        self.measure_samples = int(measure_s * sample_rate)
        self.min_confidence = min_confidence
        self.max_attempts = max_attempts
        self.on_delay = on_delay
        self.aec = EchoCanceller(sample_rate)
        self.latency_s = self.aec.n / sample_rate  # o AEC atrasa a saída em um bloco
        self.delay_ms: float | None = None
        self._ring: deque[tuple[bytes, float]] = deque(maxlen=max(1, round(pre_roll_s * sample_rate / frame_samples)))
        self._mic: list[np.ndarray] = []
        self._ref: list[np.ndarray] = []
        self._attempts = 0
        self._hold_until = float("-inf")
        if delay_ms is not None:
            self._set_delay(delay_ms, None)

    def process(self, mic: bytes, ref: bytes, captured_at: float, active: bool) -> tuple[bytes, float, float | None]:
        """Um bloco do microfone e da referência → (áudio sem eco, instante de captura dele, nota
        do detector se disparou)."""
        m = np.frombuffer(mic, "<i2")
        r = np.frombuffer(ref, "<i2")
        if self.delay_ms is None:
            self._measure(m, r)
        out = np.clip(np.round(self.aec.process(m, r)), -32768, 32767).astype("<i2")
        pcm, at = out.tobytes(), captured_at - self.latency_s
        self._ring.append((pcm, at))
        score = self.detector.feed(out)
        fired = None
        if active and self.delay_ms is not None and at >= self._hold_until and score >= self.threshold:
            fired = score
            self.hold(at)
        return pcm, at, fired

    def pre_roll(self) -> list[tuple[bytes, float]]:
        """O áudio limpo dos últimos `pre_roll_s` ainda não enviado (inclui o bloco do disparo)."""
        frames = list(self._ring)
        self._ring.clear()
        return frames

    def sent(self) -> None:
        """O bloco mais recente foi enviado ao servidor: não pode voltar no pre-roll."""
        self._ring.clear()

    def hold(self, now: float) -> None:
        """Ignora disparos por `refractory_s` (a nota fica alta por vários blocos; e depois de retomar)."""
        self._hold_until = now + self.refractory_s

    def _measure(self, m: np.ndarray, r: np.ndarray) -> None:
        if not self._mic and not np.any(np.abs(r) > REF_ACTIVE):
            return  # espera ela começar a falar
        self._mic.append(m)
        self._ref.append(r)
        if sum(len(x) for x in self._mic) < self.measure_samples:
            return
        delay, confidence = estimate_delay(np.concatenate(self._mic), np.concatenate(self._ref), self.sample_rate)
        self._mic, self._ref = [], []
        self._attempts += 1
        if confidence >= self.min_confidence:
            self._set_delay(delay, confidence)
        elif self._attempts >= self.max_attempts:
            self._set_delay(0.0, None)  # sem eco audível (fone de ouvido?): o detector ouve direto

    def _set_delay(self, delay_ms: float, confidence: float | None) -> None:
        self.delay_ms = delay_ms
        # o filtro começa 16 ms antes do atraso: ele não modela eco que chegue antes (ADR-015)
        self.aec.set_delay(max(0.0, delay_ms - 16))
        if self.on_delay is not None:
            self.on_delay(delay_ms, confidence)
