"""Áudio do cliente: captura (microfone ou WAV) e reprodução das respostas."""

import asyncio
import threading
import time
from collections.abc import AsyncIterator

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # 32 ms, o bloco nativo do VAD do servidor
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


class FatalError(Exception):
    pass


def sounddevice():
    try:
        import sounddevice
    except OSError as e:  # PortAudio ausente; não confundir com erro de rede
        raise FatalError(f"não foi possível carregar o áudio (PortAudio): {e}")
    return sounddevice


def list_devices() -> None:
    sd = sounddevice()
    default_in, default_out = sd.default.device
    hostapis = sd.query_hostapis()
    for kind, key, default in (("entrada", "max_input_channels", default_in),
                               ("saída", "max_output_channels", default_out)):
        print(f"Dispositivos de {kind} (* = padrão):")
        for i, dev in enumerate(sd.query_devices()):
            if dev[key] > 0:
                mark = "*" if i == default else " "
                print(f" {mark} {i:3d}  {dev['name']}  [{hostapis[dev['hostapi']]['name']}]")
        print()


# --- Captura -------------------------------------------------------------------------

class MicSource:
    """Microfone em blocos de 512 amostras s16le, 16 kHz, mono, com o instante de captura.

    O PortAudio chama o callback numa thread própria; os blocos passam para o asyncio por uma fila.
    """

    finite = False

    def __init__(self, device: int | str | None):
        self.device = device
        sd = sounddevice()
        try:  # falha logo, antes de conectar, se o microfone não aceitar 16 kHz mono
            sd.check_input_settings(device=device, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        except (sd.PortAudioError, ValueError) as e:
            raise FatalError(
                f"o microfone não aceita 16 kHz mono: {e}\n"
                "Veja os dispositivos com --list-devices e escolha outro com --device "
                "(as entradas 'MME' costumam aceitar 16 kHz)."
            )

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        sd = sounddevice()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue()
        latency = 0.0

        def callback(indata, frames, time_info, status):
            # o fim do bloco foi captado ~`latency` antes de o callback rodar
            captured_at = time.monotonic() - latency
            if status.input_overflow:
                loop.call_soon_threadsafe(print, "  (aviso: perda de áudio na captura)")
            loop.call_soon_threadsafe(queue.put_nowait, (bytes(indata), captured_at))

        try:
            stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, channels=1,
                                       dtype="int16", device=self.device, callback=callback)
        except sd.PortAudioError as e:
            raise FatalError(f"não foi possível abrir o microfone: {e}")
        with stream:
            latency = stream.latency
            while True:
                yield await queue.get()


class WavSource:
    """Arquivo WAV de 16 kHz enviado no ritmo real, seguido de 1,5 s de silêncio."""

    finite = True

    def __init__(self, path: str):
        try:
            import numpy as np
            import soundfile as sf
        except ImportError:
            raise FatalError("--wav precisa do soundfile: pip install -e \".[wav]\"")
        # Lê em float e converte à mão (pedir int16 de um WAV float não reescala).
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate != SAMPLE_RATE:
            raise FatalError(f"{path}: {rate} Hz (esperado {SAMPLE_RATE} Hz)")
        pcm = np.clip(np.round(audio.mean(axis=1) * 32768), -32768, 32767).astype("<i2").tobytes()
        pcm += bytes(2 * int(SAMPLE_RATE * 1.5))
        step = 2 * FRAME_SAMPLES
        self._chunks = [pcm[i : i + step] for i in range(0, len(pcm) - len(pcm) % step, step)]

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        next_at = time.monotonic()
        for chunk in self._chunks:
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            yield chunk, time.monotonic()
            # se o consumidor pausou (resposta em andamento), retoma o ritmo sem rajada
            next_at = max(next_at + FRAME_S, time.monotonic())


# --- Reprodução ----------------------------------------------------------------------

class Player:
    """Saída de som aberta durante a sessão inteira (abrir por resposta custaria latência).

    O callback do PortAudio consome o buffer e toca silêncio quando ele está vazio.
    """

    def __init__(self, device: int | str | None, sample_rate: int):
        sd = sounddevice()
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._loop = asyncio.get_running_loop()
        self._first_played_at: float | None = None
        self._ending = False
        self._drained: asyncio.Event | None = None
        try:
            self._stream = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16",
                                              device=device, latency="low", callback=self._callback)
        except sd.PortAudioError as e:
            raise FatalError(f"não foi possível abrir a saída de som a {sample_rate} Hz: {e}")
        self._stream.start()
        self.latency = self._stream.latency

    def begin(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._first_played_at = None
            self._ending = False
        self._drained = asyncio.Event()

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer += pcm

    def end(self) -> None:
        with self._lock:
            self._ending = True
            empty = not self._buffer
        if empty:
            self._drained.set()

    async def wait_done(self) -> float | None:
        """Espera tocar tudo; devolve o instante (monotônico) em que o primeiro som saiu."""
        await self._drained.wait()
        await asyncio.sleep(self.latency)  # o que já foi entregue ainda está saindo na placa
        return self._first_played_at

    def _callback(self, outdata, frames, time_info, status):
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buffer[:n])
            del self._buffer[:n]
            if chunk and self._first_played_at is None:
                # o que entra no buffer agora sai no alto-falante depois da latência de saída
                self._first_played_at = time.monotonic() + self.latency
            drained = self._ending and not self._buffer
        outdata[: len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))
        if drained and self._drained is not None and not self._drained.is_set():
            self._loop.call_soon_threadsafe(self._drained.set)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class NullPlayer:
    """Sem saída de som (--no-playback): simula a reprodução pelo tempo do áudio."""

    latency = 0.0

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate

    def begin(self) -> None:
        self._first_at: float | None = None
        self._bytes = 0

    def feed(self, pcm: bytes) -> None:
        if self._first_at is None:
            self._first_at = time.monotonic()
        self._bytes += len(pcm)

    def end(self) -> None:
        pass

    async def wait_done(self) -> float | None:
        if self._first_at is not None:
            remaining = self._first_at + self._bytes / 2 / self.sample_rate - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
        return self._first_at

    def close(self) -> None:
        pass
