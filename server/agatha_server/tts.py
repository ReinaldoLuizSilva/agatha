"""Síntese de voz na CPU: Piper (ADR-005, voz "dii") ou Kokoro (ADR-019, voz "pf_dora").

Os dois devolvem PCM s16le mono na taxa nativa da voz (Piper dii 22 050 Hz, Kokoro 24 000 Hz); o
protocolo declara a taxa e o barge-in reamostra para 16 kHz (`audio.resample`). Qual motor usar
vem de `AGATHA_TTS_ENGINE`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TTSConfig:
    engine: str = "piper"  # "piper" ou "kokoro"
    voice: str = "/dados/agatha/models/tts/piper/dii_pt-BR.onnx"
    length_scale: float | None = None  # Piper: > 1 fala mais devagar; None usa o padrão da voz
    # Medido com o LLM gerando em paralelo: 3 threads → ~116 ms por frase no Piper; o padrão (4)
    # disputa a CPU com o llama-server e sobe para ~224 ms.
    threads: int = 3
    # Kokoro v1.0 (Apache 2.0) pelo kokoro-onnx. fp32: no i5-7400 o int8 não ganha nada (sem VNNI,
    # ADR-005) e o fp16 sem GPU é mais lento.
    kokoro_model: str = "/dados/agatha/models/tts/kokoro/kokoro-v1.0.onnx"
    kokoro_voices: str = "/dados/agatha/models/tts/kokoro/voices-v1.0.bin"
    kokoro_voice: str = "pf_dora"
    kokoro_speed: float = 1.0


class PiperSynthesizer:
    """Mantém a voz carregada e devolve PCM s16le mono na taxa nativa da voz."""

    def __init__(self, config: TTSConfig):
        import onnxruntime
        from piper import PiperVoice, SynthesisConfig

        self.config = config
        self.description = f"Piper {config.voice}"
        self._voice = PiperVoice.load(config.voice)
        # O Piper não expõe o número de threads; troca a sessão ONNX por uma configurada.
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = config.threads
        opts.inter_op_num_threads = 1
        self._voice.session = onnxruntime.InferenceSession(
            config.voice, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._syn_config = SynthesisConfig(length_scale=config.length_scale)
        self.sample_rate: int = self._voice.config.sample_rate

    def warmup(self) -> None:
        self.synthesize("Pronto.")

    def synthesize(self, text: str) -> bytes:
        return b"".join(
            chunk.audio_int16_bytes for chunk in self._voice.synthesize(text, self._syn_config)
        )


class KokoroSynthesizer:
    """Kokoro na CPU. O custo cresce com o tamanho do áudio (RTF ~0,4–0,6, sem piso pequeno), por
    isso o primeiro trecho curto da resposta (Chunker) pesa no primeiro som."""

    sample_rate = 24000

    def __init__(self, config: TTSConfig):
        import onnxruntime
        from kokoro_onnx import Kokoro

        self.config = config
        self.description = f"Kokoro {config.kokoro_voice} ({config.kokoro_model})"
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = config.threads
        opts.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(config.kokoro_model, sess_options=opts,
                                               providers=["CPUExecutionProvider"])
        self._kokoro = Kokoro.from_session(session, config.kokoro_voices)

    def warmup(self) -> None:
        self.synthesize("Pronto.")

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        audio, rate = self._kokoro.create(text, voice=self.config.kokoro_voice, speed=self.config.kokoro_speed,
                                          lang="pt-br")
        if rate != self.sample_rate:
            raise RuntimeError(f"Kokoro devolveu {rate} Hz, esperado {self.sample_rate} Hz")
        return np.clip(np.round(audio * 32767), -32768, 32767).astype("<i2").tobytes()


def Synthesizer(config: TTSConfig) -> PiperSynthesizer | KokoroSynthesizer:  # noqa: N802 (nome antigo da classe)
    if config.engine == "piper":
        return PiperSynthesizer(config)
    if config.engine == "kokoro":
        return KokoroSynthesizer(config)
    raise ValueError(f"AGATHA_TTS_ENGINE inválido: {config.engine!r} (use piper ou kokoro)")
