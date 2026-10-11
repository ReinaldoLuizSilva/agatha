import csv
import os
import shutil

import numpy as np
import pytest

from agatha_server.stt import STTConfig, Transcriber

FLEURS = "/dados/agatha/data/fleurs/pt_br"

pytestmark = pytest.mark.skipif(shutil.which("nvidia-smi") is None, reason="sem GPU NVIDIA")


@pytest.fixture(scope="module")
def transcriber():
    t = Transcriber(STTConfig())
    t.warmup()
    return t


def test_silencio_nao_gera_frase_longa(transcriber):
    # O Whisper às vezes "alucina" em silêncio; aqui só garantimos que não inventa um parágrafo.
    assert len(transcriber.transcribe(np.zeros(16000 * 2, dtype=np.float32)).split()) <= 3


def test_transcreve_frase_do_fleurs(transcriber):
    sf = pytest.importorskip("soundfile")
    if not os.path.exists(f"{FLEURS}/dev.tsv"):
        pytest.skip("FLEURS não encontrado")
    with open(f"{FLEURS}/dev.tsv", newline="", encoding="utf-8") as f:
        row = next(csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    audio, _ = sf.read(f"{FLEURS}/dev/{row[1]}", dtype="float32")
    text = transcriber.transcribe(audio).lower()
    reference = set(row[3].split())  # transcrição normalizada (minúsculas, sem pontuação)
    hits = sum(word.strip(".,;:!?") in reference for word in text.split())
    assert hits / len(reference) > 0.7, text
