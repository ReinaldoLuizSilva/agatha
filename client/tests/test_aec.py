"""Testes do cancelamento de eco linear (agatha_client.aec) com eco sintético."""

import numpy as np
import pytest

from agatha_client.aec import EchoCanceller

SR = 16000


def room(x: np.ndarray, delay: int = 0) -> np.ndarray:
    """Eco sintético: atraso + caminho direto + duas reflexões."""
    rir = np.zeros(delay + 800)
    rir[delay + 40], rir[delay + 200], rir[delay + 600] = 0.8, -0.3, 0.1
    return np.convolve(x, rir)[: len(x)]


def run(ec: EchoCanceller, mic: np.ndarray, ref: np.ndarray, chunk: int = 160) -> np.ndarray:
    return np.concatenate([ec.process(mic[i : i + chunk], ref[i : i + chunk]) for i in range(0, len(mic), chunk)])


def erle(mic: np.ndarray, out: np.ndarray) -> float:
    return 10 * np.log10(np.mean(mic ** 2) / (np.mean(out ** 2) + 1e-9))


def test_cancels_echo_after_convergence():
    ref = np.random.default_rng(0).normal(0, 3000, SR * 4)
    mic = room(ref)
    out = run(EchoCanceller(), mic, ref)
    assert erle(mic[-SR:], out[-SR:]) > 30


def test_fixed_delay_beyond_the_filter_tail():
    # 300 ms de atraso: fora dos 128 ms do filtro, só funciona com delay_ms
    ref = np.random.default_rng(1).normal(0, 3000, SR * 4)
    mic = room(ref, delay=int(0.3 * SR))
    assert erle(mic[-SR:], run(EchoCanceller(), mic, ref)[-SR:]) < 3
    assert erle(mic[-SR:], run(EchoCanceller(delay_ms=290), mic, ref)[-SR:]) > 25


def test_keeps_the_voice_over_the_echo():
    rng = np.random.default_rng(2)
    ref = rng.normal(0, 3000, SR * 4)
    t = np.arange(SR) / SR
    voice = np.zeros(SR * 4)
    voice[-SR:] = 1500 * np.sin(2 * np.pi * 300 * t)  # "fala" por cima, no último segundo
    mic = room(ref) + voice
    out = run(EchoCanceller(), mic, ref)
    # a saída (atrasada 1 bloco de 256) é quase só a voz
    residual = out[-SR + 400 :] - voice[-SR + 400 - 256 : -256]
    assert np.mean(residual ** 2) < 0.1 * np.mean(voice[-SR:] ** 2)


def test_silent_reference_passes_the_microphone_through():
    mic = np.random.default_rng(3).normal(0, 1000, SR)
    out = run(EchoCanceller(), mic, np.zeros(SR))
    assert np.allclose(out[256:], mic[:-256])  # latência fixa de um bloco
    assert not out[:256].any()


def test_any_chunk_size_and_reset():
    ref = np.random.default_rng(4).normal(0, 3000, SR * 3)
    mic = room(ref)
    ec = EchoCanceller()
    out = run(ec, mic, ref, chunk=1000)  # não é múltiplo do bloco
    assert len(out) == len(mic)
    assert erle(mic[-SR:], out[-SR:]) > 20
    ec.reset()
    fresh = run(ec, mic[:SR], ref[:SR], chunk=1024)
    assert erle(mic[:SR], fresh) < erle(mic[-SR:], out[-SR:])  # esqueceu o filtro


def test_sizes_must_match():
    with pytest.raises(ValueError):
        EchoCanceller().process(np.zeros(160), np.zeros(100))


def test_estimates_the_echo_delay():
    from agatha_client.aec import estimate_delay

    rng = np.random.default_rng(3)
    ref = rng.normal(0, 3000, SR * 2)
    mic = room(ref, delay=int(0.25 * SR)) + rng.normal(0, 300, SR * 2)  # caminho direto em 250 + 2,5 ms
    delay, confidence = estimate_delay(mic, ref)
    assert abs(delay - 252.5) < 1
    assert confidence > 10


def test_low_confidence_without_echo():
    from agatha_client.aec import estimate_delay

    rng = np.random.default_rng(4)
    _, confidence = estimate_delay(rng.normal(0, 300, SR * 2), rng.normal(0, 3000, SR * 2))
    assert confidence < 10


def test_set_delay_restarts_the_filter():
    ref = np.random.default_rng(5).normal(0, 3000, SR * 4)
    mic = room(ref, delay=int(0.3 * SR))
    ec = EchoCanceller()
    run(ec, mic[: SR], ref[: SR])  # aprende algo com o atraso errado
    ec.set_delay(290)
    assert erle(mic[-SR:], run(ec, mic, ref)[-SR:]) > 25
