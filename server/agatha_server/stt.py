"""Transcrição com faster-whisper (ver ADR-002: modelo `small`, GPU, int8)."""

import ctypes
import glob
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class STTConfig:
    model: str = "small"
    device: str = "cuda"
    compute_type: str = "int8"
    beam_size: int = 1
    language: str = "pt"
    models_dir: str = "/dados/agatha/models/whisper"
    cpu_threads: int = 4
    initial_prompt: str | None = None  # vocabulário para ajudar com jargão (plano B do ADR-002)


def preload_cuda_libs() -> None:
    """Carrega o cuBLAS e o cuDNN instalados via pip (extra `gpu`), se existirem.

    O CTranslate2 abre essas bibliotecas com dlopen pelo nome (ex.: libcublas.so.12). Como o
    LD_LIBRARY_PATH só é lido na partida do processo, carregamos os arquivos pelo caminho completo
    com RTLD_GLOBAL; o dlopen posterior reaproveita as que já estão na memória.
    """
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        log.info("bibliotecas CUDA via pip não encontradas; usando as do sistema")
        return

    libs = []
    for pkg in (nvidia.cublas, nvidia.cudnn):
        libs += sorted(glob.glob(str(Path(pkg.__path__[0]) / "lib" / "lib*.so.*")))

    # Algumas dependem de outras do mesmo conjunto: repete até não haver progresso.
    pending = libs
    while pending:
        failed = []
        for path in pending:
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                failed.append(path)
        if len(failed) == len(pending):
            raise OSError(f"não foi possível carregar: {', '.join(Path(p).name for p in failed)}")
        pending = failed
    log.debug("bibliotecas CUDA carregadas: %s", ", ".join(Path(p).name for p in libs))


class Transcriber:
    """Mantém o modelo carregado e transcreve frases (áudio float32 mono 16 kHz)."""

    def __init__(self, config: STTConfig):
        from faster_whisper import WhisperModel

        self.config = config
        if config.device == "cuda":
            preload_cuda_libs()
        self._model = WhisperModel(
            config.model,
            device=config.device,
            compute_type=config.compute_type,
            cpu_threads=config.cpu_threads,
            download_root=config.models_dir,
        )

    def warmup(self) -> None:
        """Primeira inferência fora do caminho crítico (inicializa kernels e alocador)."""
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            audio,
            language=self.config.language,
            beam_size=self.config.beam_size,
            initial_prompt=self.config.initial_prompt,
            vad_filter=False,  # a segmentação já foi feita pelo Segmenter
            without_timestamps=True,
            condition_on_previous_text=False,
        )
        return "".join(s.text for s in segments).strip()  # o gerador só decodifica ao ser consumido
