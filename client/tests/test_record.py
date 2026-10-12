"""Testes do gravador guiado (roteiro, WAV, reamostragem, níveis e retomada pelo manifest)."""

import json

import numpy as np
import pytest

from agatha_client import record
from agatha_client.record import (
    Take, build_plan, levels, level_warning, load_done, read_wav, resample, write_wav,
)


def tone(freq, rate, seconds=1.0, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return np.round(amp * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")


def dominant_freq(pcm, rate):
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    return np.fft.rfftfreq(len(pcm), 1 / rate)[spectrum.argmax()]


def test_plan_ids_unique_and_labels():
    plan = build_plan(["perto", "variado", "longe", "negativas", "ambiente"], ambient_min=3)
    assert len({t.id for t in plan}) == len(plan)
    by_part = {p: [t for t in plan if t.part == p] for p in ("perto", "variado", "longe", "negativas", "ambiente")}
    assert len(by_part["perto"]) == len(record.NEAR) * record.NEAR_REPEATS
    assert all(t.label == "wake" and "Agatha" in t.text for t in by_part["perto"] + by_part["longe"])
    assert all(t.label == "no_wake" and "agatha" not in t.text.lower() for t in by_part["negativas"])
    assert [t.duration_s for t in by_part["ambiente"]] == [60, 60, 60]
    assert by_part["perto"][0].id == "perto-001"


def test_speech_duration_grows_with_words():
    assert record.speech_duration("Agatha") == 2.5
    assert record.speech_duration("Agatha, quanto é quinze por cento de duzentos?") > 4


def test_plan_eco_alternates_interruptions(tmp_path):
    replies = []
    for i in range(4):
        path = tmp_path / f"resposta-{i}.wav"
        write_wav(path, np.zeros(22050 * 2, dtype="<i2"), rate=22050)
        replies.append(path)
    plan = build_plan(["eco"], replies)
    assert [t.label for t in plan] == ["echo_wake", "echo_only", "echo_wake", "echo_only"]
    assert plan[1].text is None
    assert plan[0].duration_s == 2 + record.ECO_TAIL_S
    assert plan[0].reply == "resposta-0.wav"


def test_unknown_part():
    with pytest.raises(ValueError):
        build_plan(["cozinha"])


def test_wav_roundtrip(tmp_path):
    pcm = tone(440, 16000)
    path = tmp_path / "sub" / "a.wav"
    write_wav(path, pcm)
    back, rate = read_wav(path)
    assert rate == 16000
    assert np.array_equal(back, pcm)


def test_resample_keeps_voice_band_and_cuts_above_nyquist():
    pcm = tone(1000, 22050)
    out = resample(pcm, 22050, 16000)
    assert abs(len(out) - 16000) <= 1
    assert abs(dominant_freq(out, 16000) - 1000) < 5
    # 10 kHz não cabe em 16 kHz (Nyquist 8 kHz): sem o filtro viraria um tom de 6 kHz
    high = resample(tone(10000, 22050), 22050, 16000)
    assert np.abs(high[200:-200]).max() < 0.05 * 32767


def test_resample_same_rate_is_identity():
    pcm = tone(300, 16000)
    assert np.array_equal(resample(pcm, 16000, 16000), pcm)


def test_levels_and_warnings():
    loud = tone(500, 16000, amp=1.0)
    peak, rms = levels(loud)
    assert peak > -0.1
    assert level_warning(peak, rms, "wake").startswith("estourou")
    quiet = tone(500, 16000, amp=0.001)
    peak, rms = levels(quiet)
    assert level_warning(peak, rms, "wake").startswith("muito baixo")
    assert level_warning(peak, rms, "ambient") is None  # ambiente pode ser baixo
    assert levels(np.zeros(0, dtype="<i2")) == (-120.0, -120.0)


class FakeSD:
    PortAudioError = Exception

    def __init__(self):
        self.played = []

    def check_input_settings(self, **kwargs):
        pass

    def query_devices(self, device, kind):
        return {"name": f"falso ({kind})"}

    def rec(self, frames, samplerate, channels, dtype, device):
        return np.full((frames, 1), 1000, dtype="<i2")

    def playrec(self, data, samplerate, channels, dtype, device):
        self.played.append(data)
        return np.full((len(data), 1), 1000, dtype="<i2")

    def wait(self):
        pass


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    fake = FakeSD()
    monkeypatch.setattr(record, "sounddevice", lambda: fake)
    monkeypatch.setattr(record.time, "sleep", lambda s: None)
    clock = iter(range(0, 10**6, 100))  # cada leitura do relógio avança 100 s: grava "na hora"
    monkeypatch.setattr(record.time, "monotonic", lambda: next(clock))
    return record.Recorder(tmp_path, None, None, tmp_path)


def answers(monkeypatch, *keys):
    keys = iter(keys)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(keys))


def test_run_saves_skips_and_resumes(recorder, tmp_path, monkeypatch):
    plan = [Take(f"perto-00{i}", "perto", "wake", "Agatha", "voz normal", 2.5) for i in (1, 2, 3)]
    answers(monkeypatch, "", "p", "q")  # grava a 1ª, pula a 2ª, sai na 3ª
    recorder.run(plan)
    assert load_done(tmp_path / "manifest.jsonl") == {"perto-001"}
    pcm, rate = read_wav(tmp_path / "perto" / "perto-001.wav")
    assert len(pcm) == int(2.5 * 16000)

    answers(monkeypatch, "", "")  # de novo: só o que falta
    recorder.run(plan)
    assert load_done(tmp_path / "manifest.jsonl") == {"perto-001", "perto-002", "perto-003"}
    entry = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["file"] == "perto/perto-001.wav"
    assert entry["text"] == "Agatha" and entry["device"] == "falso (input)"


def test_run_eco_saves_reference(recorder, tmp_path, monkeypatch):
    write_wav(tmp_path / "resposta-1.wav", tone(800, 22050, seconds=1.0), rate=22050)
    plan = build_plan(["eco"], [tmp_path / "resposta-1.wav"])
    answers(monkeypatch, "")
    recorder.run(plan)
    ref, _ = read_wav(tmp_path / "eco" / "eco-001.ref.wav")
    mic, _ = read_wav(tmp_path / "eco" / "eco-001.wav")
    assert len(ref) == len(mic) == int(plan[0].duration_s * 16000)
    assert abs(dominant_freq(ref, 16000) - 800) < 5  # a resposta, já em 16 kHz
    assert not ref[-int(record.ECO_TAIL_S * 16000) + 10:].any()  # cauda em silêncio
    entry = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert entry["ref_file"] == "eco/eco-001.ref.wav" and entry["label"] == "echo_wake"


def test_run_rerecords_after_level_warning(recorder, tmp_path, monkeypatch):
    plan = [Take("perto-001", "perto", "wake", "Agatha", "voz normal", 2.5)]
    monkeypatch.setattr(record, "level_warning", lambda *a: "muito baixo")
    answers(monkeypatch, "", "s", "", "n")  # grava, aceita regravar, grava de novo, desiste
    recorder.run(plan)
    lines = (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_eco_duplex_error_is_explained(recorder, tmp_path, monkeypatch):
    write_wav(tmp_path / "resposta-1.wav", tone(800, 22050), rate=22050)
    plan = build_plan(["eco"], [tmp_path / "resposta-1.wav"])

    def fail(*args, **kwargs):
        raise FakeSD.PortAudioError("Illegal combination of I/O devices")

    monkeypatch.setattr(recorder.sd, "playrec", fail)
    answers(monkeypatch, "")
    with pytest.raises(record.FatalError, match="mesmo tipo"):
        recorder.run(plan)
