import glob
import os

import numpy as np
import pytest

from agatha_server.vad import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Segmenter,
    SileroVAD,
    SpeechEnd,
    SpeechStart,
    VADConfig,
)

FLEURS_DIR = "/dados/agatha/data/fleurs/pt_br/dev"


class ScriptedVAD:
    """VAD falso: devolve as probabilidades de um roteiro, uma por bloco."""

    def __init__(self, probs: list[float]):
        self._probs = iter(probs)

    def __call__(self, frame: np.ndarray) -> float:
        return next(self._probs)


class FakeClock:
    """Relógio que avança 32 ms a cada leitura, como num fluxo em tempo real."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        self.t += FRAME_MS / 1000
        return self.t


def run(probs: list[float], config: VADConfig = VADConfig(), chunk: int = FRAME_SAMPLES):
    seg = Segmenter(config, ScriptedVAD(probs), clock=FakeClock())
    audio = np.zeros(len(probs) * FRAME_SAMPLES, dtype=np.float32)
    events = []
    for i in range(0, len(audio), chunk):
        events += seg.feed(audio[i : i + chunk])
    return events


def test_frase_simples():
    # 10 blocos de silêncio, 20 de fala (640 ms), 20 de silêncio
    events = run([0.0] * 10 + [0.9] * 20 + [0.0] * 20)
    assert len(events) == 2
    start, end = events
    assert start == SpeechStart(segment=1, audio_ms=10 * FRAME_MS)
    assert isinstance(end, SpeechEnd)
    assert end.segment == 1
    assert end.reason == "silence"
    assert end.speech_ms == 20 * FRAME_MS
    # fim detectado após 16 blocos de silêncio (16 × 32 = 512 ≥ 500 ms)
    assert end.audio_ms == (10 + 20 + 16) * FRAME_MS
    # pre-roll (9 blocos = 288 ms) + fala + silêncio final
    assert len(end.audio) == (9 + 20 + 16) * FRAME_SAMPLES
    assert end.endpoint_ms == pytest.approx(16 * FRAME_MS)


def test_start_so_apos_min_speech():
    events = run([0.0] * 5 + [0.9] * 20 + [0.0] * 20)
    # o start sai no 8º bloco de fala (8 × 32 = 256 ≥ 250 ms), mas aponta o início real
    assert events[0] == SpeechStart(segment=1, audio_ms=5 * FRAME_MS)


def test_estalo_curto_e_descartado():
    assert run([0.0] * 5 + [0.9] * 3 + [0.0] * 20) == []


def test_histerese_mantem_fala_entre_limiares():
    # 0,4 está entre o neg_threshold (0,35) e o threshold (0,5): continua sendo fala
    events = run([0.9] * 10 + [0.4] * 30 + [0.0] * 20)
    end = events[-1]
    assert end.speech_ms == 40 * FRAME_MS


def test_pausa_curta_nao_encerra_frase():
    # pausa de 10 blocos (320 ms) < 500 ms: uma frase só
    events = run([0.9] * 10 + [0.0] * 10 + [0.9] * 10 + [0.0] * 20)
    assert [type(e) for e in events] == [SpeechStart, SpeechEnd]
    assert events[1].speech_ms == 30 * FRAME_MS


def test_duas_frases():
    events = run([0.9] * 10 + [0.0] * 20 + [0.9] * 10 + [0.0] * 20)
    assert [(type(e), e.segment) for e in events] == [
        (SpeechStart, 1), (SpeechEnd, 1), (SpeechStart, 2), (SpeechEnd, 2)
    ]


def test_corte_por_tamanho_maximo():
    config = VADConfig(max_segment_s=1.0)  # 31 blocos
    events = run([0.9] * 40 + [0.0] * 20, config)
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    assert ends[0].reason == "max_length"
    assert ends[0].audio_ms == 31 * FRAME_MS
    # a fala continua: os 9 blocos restantes formam uma segunda frase
    assert ends[1].reason == "silence"
    assert ends[1].segment == 2


def test_speech_end_ms():
    events = run([0.0] * 10 + [0.9] * 20 + [0.0] * 20)
    assert events[1].speech_end_ms == (10 + 20) * FRAME_MS


def test_discard_e_reset_avancam_a_posicao():
    seg = Segmenter(VADConfig(), ScriptedVAD([0.9] * 5 + [0.0] * 3 + [0.9] * 20 + [0.0] * 20), clock=FakeClock())
    silence = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    # começa uma frase (5 blocos de fala) e é interrompida por uma resposta
    assert seg.feed(np.zeros(5 * FRAME_SAMPLES + 100, dtype=np.float32)) == []
    seg.reset()  # descarta a frase e os 100 amostras pendentes
    seg.discard(16000)  # 1 s ignorado durante a resposta
    events = []
    for _ in range(3 + 20 + 20):
        events += seg.feed(silence)
    start = events[0]
    # 5 blocos processados + 100 pendentes + 16000 descartadas + 3 blocos de silêncio
    expected = (5 * FRAME_SAMPLES + 100 + 16000 + 3 * FRAME_SAMPLES) * 1000 // 16000
    assert start == SpeechStart(segment=1, audio_ms=expected)


@pytest.mark.parametrize("chunk", [100, 512, 1000, 4096])
def test_tamanho_do_pedaco_nao_muda_resultado(chunk):
    probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 20
    reference = run(probs)
    events = run(probs, chunk=chunk)
    assert [(type(e), e.segment, e.audio_ms) for e in events] == [
        (type(e), e.segment, e.audio_ms) for e in reference
    ]


# --- Silero real ---------------------------------------------------------------

def test_silero_silencio():
    vad = SileroVAD()
    probs = [vad(np.zeros(FRAME_SAMPLES, dtype=np.float32)) for _ in range(20)]
    assert max(probs) < 0.1


def test_silero_bloco_de_tamanho_errado():
    with pytest.raises(ValueError):
        SileroVAD()(np.zeros(100, dtype=np.float32))


def _fleurs_sample() -> str:
    files = sorted(glob.glob(os.path.join(FLEURS_DIR, "*.wav")))
    if not files:
        pytest.skip(f"FLEURS não encontrado em {FLEURS_DIR}")
    return files[0]


def _load(path: str) -> np.ndarray:
    sf = pytest.importorskip("soundfile")
    audio, rate = sf.read(path, dtype="float32")
    assert rate == 16000
    return audio[: len(audio) - len(audio) % FRAME_SAMPLES]


def test_silero_streaming_igual_ao_lote_do_faster_whisper():
    from faster_whisper.vad import get_vad_model

    audio = _load(_fleurs_sample())
    batch = get_vad_model()(audio.copy())
    vad = SileroVAD()
    stream = [vad(audio[i : i + FRAME_SAMPLES]) for i in range(0, len(audio), FRAME_SAMPLES)]
    # O último bloco fica de fora: a versão em lote do faster-whisper 1.2.1 zera, via view,
    # as últimas 64 amostras do áudio de entrada (`context[-1] = 0`), alterando só esse bloco.
    np.testing.assert_allclose(stream[:-1], np.ravel(batch)[:-1], atol=1e-4)


def test_segmenta_frase_real():
    audio = _load(_fleurs_sample())
    # silêncio no fim para garantir que a frase feche
    audio = np.concatenate([audio, np.zeros(SAMPLE_RATE, dtype=np.float32)])
    events = Segmenter(VADConfig(), SileroVAD()).feed(audio)
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    assert ends, "nenhuma frase detectada"
    assert sum(e.speech_ms for e in ends) > 1000

