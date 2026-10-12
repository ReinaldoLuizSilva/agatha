"""Ponto de entrada: python -m agatha_server"""

import asyncio
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from .app import run
from .config import ConfigError, load_config
from .llm import LLMClient
from .stt import Transcriber
from .tts import Synthesizer


def main() -> int:
    try:
        config = load_config()
    except ConfigError as e:
        print(f"erro de configuração: {e}", file=sys.stderr)
        return 2

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_dir:
        # além do stderr (podman logs), um arquivo que sobrevive à troca do container (ADR-013)
        Path(config.log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(config.log_dir) / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)  # uma linha por transcrição
    logging.getLogger("httpx").setLevel(logging.WARNING)  # uma linha por chamada ao LLM
    # o healthcheck abre uma conexão a cada 10 s; as conexões de verdade já são registradas pelo app
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    log = logging.getLogger("agatha_server")

    stt = config.stt
    log.info("carregando STT: %s (%s, %s, beam %d)", stt.model, stt.device, stt.compute_type, stt.beam_size)
    t0 = time.perf_counter()
    transcriber = Transcriber(stt)
    transcriber.warmup()
    log.info("STT pronto em %.1f s", time.perf_counter() - t0)

    log.info("carregando TTS: %s", config.tts.voice)
    t0 = time.perf_counter()
    synthesizer = Synthesizer(config.tts)
    synthesizer.warmup()
    log.info("TTS pronto em %.1f s (%d Hz)", time.perf_counter() - t0, synthesizer.sample_rate)

    asyncio.run(run(config, transcriber, LLMClient(config.llm), synthesizer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
