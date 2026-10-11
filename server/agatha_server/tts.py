"""Síntese de voz com o Piper na CPU (ver ADR-005: voz feminina "dii")."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TTSConfig:
    voice: str = "/dados/agatha/models/tts/piper/dii_pt-BR.onnx"
    length_scale: float | None = None  # > 1 fala mais devagar; None usa o padrão da voz
    # Medido com o LLM gerando em paralelo: 3 threads → ~116 ms por frase; o padrão (4) disputa
    # a CPU com o llama-server e sobe para ~224 ms.
    threads: int = 3


class Synthesizer:
    """Mantém a voz carregada e devolve PCM s16le mono na taxa nativa da voz."""

    def __init__(self, config: TTSConfig):
        import onnxruntime
        from piper import PiperVoice, SynthesisConfig

        self.config = config
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
