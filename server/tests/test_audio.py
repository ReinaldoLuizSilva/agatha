import numpy as np

from agatha_server.audio import resample


def tone(freq, rate, seconds=1.0):
    t = np.arange(int(rate * seconds)) / rate
    return np.round(16000 * np.sin(2 * np.pi * freq * t)).astype("<i2")


def dominant(pcm: bytes, rate: int) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(float)
    return np.fft.rfftfreq(len(x), 1 / rate)[np.abs(np.fft.rfft(x)).argmax()]


def test_preserva_a_voz_e_a_duracao():
    out = resample(tone(1000, 22050).tobytes(), 22050, 16000)
    assert abs(len(out) // 2 - 16000) <= 1
    assert abs(dominant(out, 16000) - 1000) < 5


def test_corta_acima_da_nova_nyquist():
    out = np.frombuffer(resample(tone(10000, 22050).tobytes(), 22050, 16000), dtype="<i2")
    assert np.abs(out[200:-200]).max() < 0.05 * 16000  # sem filtro viraria um tom de 6 kHz


def test_trecho_menor_que_o_filtro():
    assert len(resample(b"\x01\x00" * 100, 22050, 16000)) // 2 == 72
    assert resample(b"", 22050, 16000) == b""


def test_mesma_taxa_nao_muda_nada():
    pcm = tone(300, 16000).tobytes()
    assert resample(pcm, 16000, 16000) is pcm
