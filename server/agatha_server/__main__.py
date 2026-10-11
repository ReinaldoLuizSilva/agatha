"""Ponto de entrada: python -m agatha_server"""

import asyncio
import logging
import sys
import time

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

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)  # uma linha por transcrição
    logging.getLogger("httpx").setLevel(logging.WARNING)  # uma linha por chamada ao LLM
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
