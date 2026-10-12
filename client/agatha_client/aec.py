"""Cancelamento de eco linear para o barge-in (Fase 5, etapa 3).

O microfone do notebook capta a Agatha saindo pelas caixas, mais alto que a voz do Reinaldo (de 0
a 10 dB acima, medido nas gravações). O AEC3 do WebRTC tira ~30 dB de eco, mas o supressor não
linear dele também apaga a voz de quem fala por cima, e o detector da wake word deixa de ouvir o
"Agatha" (31% de acerto). Aqui o cancelamento é só linear: tira menos eco, mas preserva a voz.

Filtro adaptativo em blocos no domínio da frequência (PBFDAF, overlap-save), como o do Speex:
o eco é estimado como a referência (o que foi tocado) passada por um filtro de `tail_ms`, depois
de um atraso fixo `delay_ms` (saída de som → ar → microfone). A adaptação é normalizada pela
potência da referência e congela quando a referência está em silêncio ou quando parece haver
alguém falando por cima (o erro fica bem maior que o eco estimado), para a voz não desajustar o
filtro.
"""

import numpy as np


class EchoCanceller:
    """Recebe blocos do microfone e da referência (o que foi tocado no mesmo instante).

    `process` aceita pedaços de qualquer tamanho (int16 ou float) e devolve o mesmo número de
    amostras já sem o eco, com atraso fixo de `block` amostras (16 ms com os valores padrão).
    """

    def __init__(self, sample_rate: int = 16000, block: int = 256, tail_ms: float = 128.0,
                 delay_ms: float = 0.0, mu: float = 1.0, dtd_ratio: float = 2.0):
        self.n = block
        self.parts = max(1, int(np.ceil(tail_ms * sample_rate / 1000 / block)))
        self.delay = int(round(delay_ms * sample_rate / 1000))
        self.mu = mu
        self.dtd_ratio = dtd_ratio
        self.reset()

    def reset(self) -> None:
        """Esquece o filtro e os buffers (nova sessão de áudio)."""
        bins = self.n + 1
        self._w = np.zeros((self.parts, bins), np.complex128)  # filtro, uma partição por bloco
        self._x = np.zeros((self.parts, bins), np.complex128)  # espectros recentes da referência
        self._power = np.full(bins, 1e-2)
        self._ref_prev = np.zeros(self.n)
        self._ref_queue = np.zeros(self.delay)  # atraso fixo da referência
        self._mic_buf = np.zeros(0)
        self._ref_buf = np.zeros(0)
        self._out_buf = np.zeros(self.n)  # latência fixa de um bloco: sempre há saída pronta
        self._echo_level = 0.0
        self._err_level = 0.0

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        if len(mic) != len(ref):
            raise ValueError("microfone e referência precisam ter o mesmo tamanho")
        ref = np.asarray(ref, np.float64)
        if self.delay:
            joined = np.concatenate([self._ref_queue, ref])
            ref, self._ref_queue = joined[: len(ref)], joined[len(ref) :]
        self._mic_buf = np.concatenate([self._mic_buf, np.asarray(mic, np.float64)])
        self._ref_buf = np.concatenate([self._ref_buf, ref])
        while len(self._mic_buf) >= self.n:
            d, self._mic_buf = self._mic_buf[: self.n], self._mic_buf[self.n :]
            x, self._ref_buf = self._ref_buf[: self.n], self._ref_buf[self.n :]
            self._out_buf = np.concatenate([self._out_buf, self._block(d, x)])
        out, self._out_buf = self._out_buf[: len(mic)], self._out_buf[len(mic) :]
        return out

    def _block(self, d: np.ndarray, x: np.ndarray) -> np.ndarray:
        n = self.n
        X = np.fft.rfft(np.concatenate([self._ref_prev, x]))
        self._ref_prev = x
        self._x = np.roll(self._x, 1, axis=0)
        self._x[0] = X
        y = np.fft.irfft((self._w * self._x).sum(axis=0))[n:]
        e = d - y

        self._power = 0.9 * self._power + 0.1 * np.abs(X) ** 2
        ref_energy = float(np.mean(x * x))
        self._echo_level = 0.95 * self._echo_level + 0.05 * float(np.mean(y * y))
        self._err_level = 0.95 * self._err_level + 0.05 * float(np.mean(e * e))
        # alguém falando por cima: o erro dispara em relação ao eco já estimado
        double_talk = self._echo_level > 1.0 and float(np.mean(e * e)) > self.dtd_ratio * max(
            self._err_level, self._echo_level)
        if ref_energy > 1.0 and not double_talk:
            E = np.fft.rfft(np.concatenate([np.zeros(n), e]))
            norm = self.parts * self._power + 1e-6 * self._power.max() + 1e-9
            for m in range(self.parts):
                g = np.fft.irfft(self.mu * np.conj(self._x[m]) * E / norm)
                g[n:] = 0.0  # restrição de gradiente: o filtro continua com n coeficientes
                self._w[m] += np.fft.rfft(g)
        return e
