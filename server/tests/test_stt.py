import csv
import os
import shutil
import subprocess

import numpy as np
import pytest

from agatha_server.stt import STTConfig, Transcriber

FLEURS = "/dados/agatha/data/fleurs/pt_br"


def _free_vram_mb() -> int:
    if shutil.which("nvidia-smi") is None:
        return 0
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=False).stdout
    return int(out.split()[0]) if out.strip() else 0


# O modelo de teste precisa de ~0,4 GB; com o servidor e o LLM no ar a GPU fica quase cheia.
pytestmark = pytest.mark.skipif(
    _free_vram_mb() < 700, reason="GPU ausente ou sem VRAM livre (pare o servidor e o agatha-llm)"
)


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
