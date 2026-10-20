"""Testes do barge-in no cliente (agatha_client.bargein) com eco sintético e detector falso."""

import numpy as np

from agatha_client.bargein import BargeIn

SR = 16000
FRAME = 512


class FakeDetector:
    """Dá a nota `score` a partir do bloco `fire_at` (contado em blocos recebidos)."""

    def __init__(self, fire_at: int = 10**9, score: float = 0.95):
        self.fire_at, self.score, self.fed = fire_at, score, 0

    def feed(self, pcm):
        self.fed += 1
        return self.score if self.fed > self.fire_at else 0.0

    def reset(self):
        self.fed = 0


def frames(seconds: float, delay_ms: float | None = 200, voice: bool = True, seed: int = 0):
    """(mic, ref) em blocos: a Agatha falando (ruído) e o eco dela chegando com `delay_ms`."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR) // FRAME * FRAME
    ref = rng.normal(0, 3000, n) if voice else np.zeros(n)
    mic = rng.normal(0, 100, n)
    if delay_ms is not None:
        d = int(delay_ms * SR / 1000)
        mic[d:] += 0.6 * ref[: n - d]
    to16 = lambda x: np.clip(np.round(x), -32768, 32767).astype("<i2")  # noqa: E731
    mic, ref = to16(mic), to16(ref)
    return [(mic[i : i + FRAME].tobytes(), ref[i : i + FRAME].tobytes()) for i in range(0, n, FRAME)]


def feed(b: BargeIn, blocks, active=True, t0=0.0):
    out = []
    for i, (mic, ref) in enumerate(blocks):
        out.append(b.process(mic, ref, t0 + i * FRAME / SR, active))
    return out


def test_mede_o_atraso_pela_fala_dela():
    delays = []
    b = BargeIn(FakeDetector(), on_delay=lambda d, c: delays.append((d, c)))
    feed(b, frames(3.0, delay_ms=200))
    assert abs(b.delay_ms - 200) < 1 and delays[0][1] > 10


def test_dispara_antes_de_medir_o_atraso():
    # com fone não há eco: a medição só desiste depois de ~6 s, e o barge-in não pode esperar por ela
    b = BargeIn(FakeDetector(fire_at=5))
    out = feed(b, frames(1.0, delay_ms=None))
    assert b.delay_ms is None and any(f is not None for _, _, f in out)


def test_so_dispara_durante_a_resposta_e_respeita_o_intervalo():
    b = BargeIn(FakeDetector(fire_at=0), delay_ms=200, refractory_s=1.5)
    assert all(f is None for _, _, f in feed(b, frames(1.0), active=False))
    fired = [i for i, (_, _, f) in enumerate(feed(b, frames(4.0), active=True, t0=1.0)) if f is not None]
    assert fired and np.all(np.diff(fired) * FRAME / SR >= 1.5)


def test_abaixo_do_limiar_nao_dispara():
    b = BargeIn(FakeDetector(fire_at=0, score=0.6), delay_ms=200, threshold=0.9)
    assert all(f is None for _, _, f in feed(b, frames(2.0)))


def test_pre_roll_tem_o_audio_ainda_nao_enviado_em_ordem():
    b = BargeIn(FakeDetector(), delay_ms=200, pre_roll_s=1.0)
    out = feed(b, frames(3.0))
    pre = b.pre_roll()
    assert len(pre) == round(SR / FRAME)  # ~1 s
    assert [p for p, _ in pre] == [p for p, _, _ in out[-len(pre):]]
    assert all(a < b for (_, a), (_, b) in zip(pre, pre[1:]))
    assert b.pre_roll() == []  # não manda duas vezes
    feed(b, frames(0.5))
    b.sent()
    assert b.pre_roll() == []  # o que já foi enviado não volta


def test_instante_de_captura_desconta_a_latencia_do_aec():
    b = BargeIn(FakeDetector(), delay_ms=200)
    _, at, _ = b.process(*frames(0.1)[0], captured_at=10.0, active=False)
    assert at == 10.0 - 256 / SR


def test_sem_eco_desiste_de_medir_e_ouve_direto():
    delays = []
    b = BargeIn(FakeDetector(), on_delay=lambda d, c: delays.append((d, c)), max_attempts=3)
    feed(b, frames(7.0, delay_ms=None))  # fone de ouvido: nada do que ela fala volta no microfone
    assert b.delay_ms == 0.0 and delays == [(0.0, None)]


def test_cancela_o_eco_depois_de_medir():
    b = BargeIn(FakeDetector())
    blocks = frames(6.0, delay_ms=200)
    out = feed(b, blocks)
    mic = np.frombuffer(b"".join(m for m, _ in blocks[-60:]), "<i2").astype(float)
    clean = np.frombuffer(b"".join(p for p, _, _ in out[-60:]), "<i2").astype(float)
    assert 10 * np.log10(np.mean(mic ** 2) / np.mean(clean ** 2)) > 15


def test_resumo_da_resposta_para_diagnostico():
    b = BargeIn(FakeDetector(fire_at=40, score=0.7), delay_ms=200, threshold=0.9)
    b.begin_stats(0.0)
    feed(b, frames(3.0, delay_ms=200))
    line = b.end_stats()
    assert line.startswith("nota máx 0.70 aos 1.3 s") and "blocos ≥ 0,5" in line
    assert "AEC tirou" in line and "dBFS" in line and "ms por bloco de 32 ms" in line
    assert b.end_stats() is None  # uma vez por resposta


def test_diagnostico_mostra_nivel_e_nota_quando_ha_som():
    lines = []
    b = BargeIn(FakeDetector(fire_at=30, score=0.3), delay_ms=200, threshold=0.9, debug=lines.append)
    b.begin_stats(0.0)
    feed(b, frames(3.0, delay_ms=200))  # o eco sintético é alto: aparece em todas as janelas
    assert len(lines) >= 4 and "dBFS (pico)" in lines[0]
    assert any("nota 0.30" in line for line in lines)
