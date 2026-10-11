"""Testes do servidor WebSocket com VAD e STT falsos (sem GPU)."""

import asyncio
import json

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from agatha_server.app import AgathaServer
from agatha_server.config import ServerConfig
from agatha_server.protocol import AUDIO_FORMAT
from agatha_server.stt import STTConfig
from agatha_server.vad import FRAME_SAMPLES, VADConfig

TOKEN = "segredo-de-teste"
CONFIG = ServerConfig(
    host="127.0.0.1", port=0, token=TOKEN, log_level="DEBUG", stt=STTConfig(), vad=VADConfig()
)


class EnergyVAD:
    """VAD falso: bloco com sinal é fala, bloco zerado é silêncio."""

    def __call__(self, frame: np.ndarray) -> float:
        return 0.9 if np.abs(frame).max() > 0 else 0.0


class FakeTranscriber:
    def transcribe(self, audio: np.ndarray) -> str:
        return f"{len(audio)} amostras"


def hello(**overrides) -> str:
    msg = {"type": "hello", "protocol": 1, "client": "teste", "token": TOKEN, "audio": AUDIO_FORMAT}
    msg.update(overrides)
    return json.dumps(msg)


def pcm(frames: int, amplitude: int) -> bytes:
    return np.full(frames * FRAME_SAMPLES, amplitude, dtype="<i2").tobytes()


def with_server(scenario, hello_timeout: float = 5.0):
    """Sobe o servidor numa porta livre, roda o cenário do cliente e devolve o resultado."""

    async def main():
        app = AgathaServer(CONFIG, FakeTranscriber(), vad_factory=EnergyVAD, hello_timeout=hello_timeout)
        async with serve(app.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/") as ws:
                return await asyncio.wait_for(scenario(ws), 10)

    return asyncio.run(main())


async def recv_json(ws) -> dict:
    return json.loads(await ws.recv())


async def expect_rejection(ws) -> tuple[dict, int]:
    error = await recv_json(ws)
    with pytest.raises(ConnectionClosed) as closed:
        await ws.recv()
    return error, closed.value.rcvd.code


def test_handshake():
    async def scenario(ws):
        await ws.send(hello())
        return await recv_json(ws)

    ready = with_server(scenario)
    assert ready["type"] == "ready"
    assert ready["protocol"] == 1
    assert ready["vad"]["min_silence_ms"] == 500


@pytest.mark.parametrize(
    "first_message, code, close_code",
    [
        (hello(token="errado"), "unauthorized", 1008),
        (hello(token=None), "unauthorized", 1008),
        (hello(protocol=2), "unsupported_protocol", 1002),
        (hello(audio={"encoding": "pcm_f32le", "sample_rate": 16000, "channels": 1}), "unsupported_audio", 1003),
        ("não é json", "bad_message", 1002),
        (json.dumps({"type": "outra"}), "bad_message", 1002),
        (b"\x00\x00", "audio_before_ready", 1008),
    ],
)
def test_handshake_recusado(first_message, code, close_code):
    async def scenario(ws):
        await ws.send(first_message)
        return await expect_rejection(ws)

    error, received_close = with_server(scenario)
    assert error == {"type": "error", "code": code, "message": error["message"]}
    assert received_close == close_code


def test_hello_timeout():
    error, close_code = with_server(expect_rejection, hello_timeout=0.2)
    assert error["code"] == "hello_timeout"
    assert close_code == 1008


def test_erros_que_nao_fecham_conexao():
    async def scenario(ws):
        await ws.send(hello())
        await recv_json(ws)  # ready
        await ws.send(b"\x00\x00\x00")  # número ímpar de bytes
        bad_audio = await recv_json(ws)
        await ws.send(json.dumps({"type": "ping"}))
        bad_message = await recv_json(ws)
        # a conexão continua funcionando
        await ws.send(pcm(10, 1000) + pcm(20, 0))
        return bad_audio, bad_message, await recv_json(ws)

    bad_audio, bad_message, vad = with_server(scenario)
    assert bad_audio["code"] == "bad_audio"
    assert bad_message["code"] == "bad_message"
    assert vad["type"] == "vad"


def test_frase_completa():
    async def scenario(ws):
        await ws.send(hello())
        await recv_json(ws)  # ready
        # 5 blocos de silêncio, 20 de "fala", 20 de silêncio, enviados bloco a bloco
        for amplitude, frames in ((0, 5), (1000, 20), (0, 20)):
            for _ in range(frames):
                await ws.send(pcm(1, amplitude))
        return [await recv_json(ws) for _ in range(3)]

    start, end, transcript = with_server(scenario)
    assert start == {"type": "vad", "event": "start", "segment": 1, "audio_ms": 5 * 32}
    assert end == {"type": "vad", "event": "end", "segment": 1, "audio_ms": (5 + 20 + 16) * 32, "reason": "silence"}
    assert transcript["type"] == "transcript"
    assert transcript["segment"] == 1
    # pre-roll de 5 blocos (só havia 5 antes da fala) + 20 de fala + 16 de silêncio
    assert transcript["text"] == f"{(5 + 20 + 16) * FRAME_SAMPLES} amostras"
    assert set(transcript["timings"]) == {"speech_ms", "endpoint_ms", "queue_ms", "stt_ms", "total_ms"}
    assert transcript["timings"]["speech_ms"] == 20 * 32
