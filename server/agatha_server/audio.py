"""Reamostragem do áudio da resposta para a taxa pedida pelo cliente.

O barge-in (ADR-015) usa microfone e saída de som no mesmo stream, numa taxa só (16 kHz). A voz
do TTS sai a 22 050 Hz; cada frase é reamostrada inteira (passa-baixa FIR + interpolação), sem
emendas no meio, então não há estalos entre os pedaços.
"""

import numpy as np

_TAPS = np.arange(-64, 65)


def resample(pcm: bytes, rate_in: int, rate_out: int) -> bytes:
    """PCM s16le mono de `rate_in` para `rate_out`."""
    if rate_in == rate_out or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if rate_out < rate_in:  # corta acima da nova Nyquist para não dobrar frequências
        cutoff = 0.45 * rate_out / rate_in
        taps = 2 * cutoff * np.sinc(2 * cutoff * _TAPS) * np.hamming(len(_TAPS))
        # "full" e corta o centro: com mode="same", um trecho menor que o filtro sairia maior
        x = np.convolve(x, taps / taps.sum())[len(_TAPS) // 2 : len(_TAPS) // 2 + len(x)]
    t_out = np.arange(int(len(x) * rate_out / rate_in)) * rate_in / rate_out
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()
