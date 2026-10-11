"""Testes do servidor WebSocket (protocolo v2) com VAD, STT e conversa falsos (sem GPU)."""

import asyncio
import dataclasses
import json
import time

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from agatha_server.app import AgathaServer
from agatha_server.config import ServerConfig
from agatha_server.conversation import Audio, ConversationConfig, Done, TextChunk
from agatha_server.llm import LLMConfig
from agatha_server.protocol import AUDIO_FORMAT
from agatha_server.stt import STTConfig
from agatha_server.tts import TTSConfig
from agatha_server.vad import FRAME_MS, FRAME_SAMPLES, VADConfig

TOKEN = "segredo-de-teste"
CONFIG = ServerConfig(
    host="127.0.0.1", port=0, token=TOKEN, log_level="DEBUG", stt=STTConfig(), vad=VADConfig(),
    llm=LLMConfig(), tts=TTSConfig(), conversation=ConversationConfig(),
)
RATE = 22050
REPLY_AUDIO = [b"\x01\x00" * 5000, b"\x02\x00" * 100]  # 1º trecho vira 2 frames (> 4096 amostras)


class EnergyVAD:
    """VAD falso: bloco com sinal é fala, bloco zerado é silêncio."""

    def __call__(self, frame: np.ndarray) -> float:
        return 0.9 if np.abs(frame).max() > 0 else 0.0


class FakeTranscriber:
    def __init__(self, text: str):
        self.text = text

    def transcribe(self, audio: np.ndarray) -> str:
        return self.text


class SequenceTranscriber:
    """Devolve um texto por chamada, na ordem; depois, texto vazio."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)

    def transcribe(self, audio: np.ndarray) -> str:
        return self.texts.pop(0) if self.texts else ""


class FakeConversation:
    heard: list[str] = []  # textos que chegaram para resposta (ordem de chegada)

    async def respond(self, user_text, t_ref):
        FakeConversation.heard.append(user_text)
        yield TextChunk("Oi, Rei.")
        yield Audio(REPLY_AUDIO[0])
        yield TextChunk("Tudo bem.")
        yield Audio(REPLY_AUDIO[1])
        yield Done("done", "Oi, Rei. Tudo bem.", {"llm_first_token_ms": 1, "first_chunk_ms": 2, "tts_first_ms": 3})


def hello(**overrides) -> str:
    msg = {"type": "hello", "protocol": 2, "client": "teste", "token": TOKEN, "audio": AUDIO_FORMAT}
    msg.update(overrides)
    return json.dumps(msg)


def pcm(frames: int, amplitude: int) -> bytes:
    return np.full(frames * FRAME_SAMPLES, amplitude, dtype="<i2").tobytes()


def utterance() -> list[bytes]:
    """5 blocos de silêncio, 20 de "fala" e 20 de silêncio, bloco a bloco (45 blocos)."""
    return [pcm(1, a) for a, n in ((0, 5), (1000, 20), (0, 20)) for _ in range(n)]


def with_server(scenario, transcript: str = "olá agatha", hello_timeout: float = 5.0, grace: float = 15.0,
                transcriber=None, continuation_ms: int = 300, conversation_s: float = 10.0):
    """Sobe o servidor numa porta livre, roda o cenário do cliente e devolve o resultado."""
    FakeConversation.heard = []
    config = dataclasses.replace(
        CONFIG,
        vad=dataclasses.replace(CONFIG.vad, continuation_ms=continuation_ms),
        wake=dataclasses.replace(CONFIG.wake, conversation_s=conversation_s),
    )

    async def main():
        app = AgathaServer(
            config, transcriber or FakeTranscriber(transcript), FakeConversation, RATE,
            vad_factory=EnergyVAD, hello_timeout=hello_timeout, playback_grace_s=grace,
        )
        async with serve(app.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/") as ws:
                return await asyncio.wait_for(scenario(ws), 10)

    return asyncio.run(main())


async def recv(ws) -> dict | bytes:
    msg = await ws.recv()
    return msg if isinstance(msg, bytes) else json.loads(msg)


async def recv_until(ws, msg_type: str) -> list[dict | bytes]:
    msgs = []
    while True:
        msg = await recv(ws)
        msgs.append(msg)
        if isinstance(msg, dict) and msg["type"] == msg_type:
            return msgs


async def start(ws) -> dict:
    await ws.send(hello())
    return await recv(ws)


async def send_all(ws, frames: list[bytes]) -> None:
    for frame in frames:
        await ws.send(frame)


def kinds(msgs: list) -> list[str]:
    return ["<audio>" if isinstance(m, bytes) else (f"vad:{m['event']}" if m["type"] == "vad" else m["type"])
            for m in msgs]


# --- Handshake --------------------------------------------------------------------------

def test_handshake():
    ready = with_server(start)
    assert ready["type"] == "ready"
    assert ready["protocol"] == 2
    assert ready["output_audio"] == {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}
    assert ready["vad"]["min_silence_ms"] == 500
    assert ready["wake"]["enabled"] is True
    assert "agatha" in ready["wake"]["words"]


async def expect_rejection(ws) -> tuple[dict, int]:
    error = await recv(ws)
    with pytest.raises(ConnectionClosed) as closed:
        await ws.recv()
    return error, closed.value.rcvd.code


@pytest.mark.parametrize(
    "first_message, code, close_code",
    [
        (hello(token="errado"), "unauthorized", 1008),
        (hello(token=None), "unauthorized", 1008),
        (hello(protocol=1), "unsupported_protocol", 1002),
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
        await start(ws)
        await ws.send(b"\x00\x00\x00")  # número ímpar de bytes
        bad_audio = await recv(ws)
        await ws.send(json.dumps({"type": "ping"}))
        bad_message = await recv(ws)
        await ws.send(json.dumps({"type": "playback_end"}))  # sem 'response'
        bad_playback = await recv(ws)
        await send_all(ws, utterance())  # a conexão continua funcionando
        return bad_audio, bad_message, bad_playback, await recv(ws)

    bad_audio, bad_message, bad_playback, vad = with_server(scenario)
    assert bad_audio["code"] == "bad_audio"
    assert bad_message["code"] == "bad_message"
    assert bad_playback["code"] == "bad_message"
    assert vad["type"] == "vad"


# --- Conversa ---------------------------------------------------------------------------

def test_conversa_completa():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        return await recv_until(ws, "response_end")

    msgs = with_server(scenario)
    assert kinds(msgs) == [
        "vad:start", "vad:end", "transcript",
        "response_start", "response_text", "audio_start", "<audio>", "<audio>",
        "response_text", "<audio>", "audio_end", "response_end",
    ]
    vad_end, transcript, response_start = msgs[1], msgs[2], msgs[3]
    assert vad_end["speech_end_ms"] == (5 + 20) * FRAME_MS
    assert vad_end["audio_ms"] == (5 + 20 + 16) * FRAME_MS
    assert transcript["text"] == "olá agatha"
    assert response_start == {"type": "response_start", "response": 1, "segment": 1}
    assert msgs[5] == {"type": "audio_start", "response": 1, "encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}
    assert b"".join(m for m in msgs if isinstance(m, bytes)) == b"".join(REPLY_AUDIO)
    end = msgs[-1]
    assert end["reason"] == "done"
    assert end["text"] == "Oi, Rei. Tudo bem."
    assert end["timings"]["audio_ms"] == round(5100 / RATE * 1000)
    assert {"stt_ms", "llm_first_token_ms", "first_chunk_ms", "tts_first_ms", "first_audio_ms"} <= set(end["timings"])


def test_half_duplex_ignora_microfone_ate_playback_end():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())  # 45 blocos
        await recv_until(ws, "response_end")
        await send_all(ws, utterance()[:30])  # fala durante a resposta: 30 blocos ignorados
        await ws.send(json.dumps({"type": "playback_end", "response": 1, "e2e_ms": 1650}))
        await send_all(ws, utterance())  # nova fala, depois do playback_end
        return await recv(ws)

    first_after = with_server(scenario)
    # nenhum evento da fala ignorada; a posição conta os blocos ignorados
    assert first_after == {"type": "vad", "event": "start", "segment": 2, "audio_ms": (45 + 30 + 5) * FRAME_MS}


def test_playback_end_de_outra_resposta_e_ignorado():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "response_end")
        await ws.send(json.dumps({"type": "playback_end", "response": 99, "e2e_ms": None}))
        await send_all(ws, utterance())  # ainda respondendo: ignorado
        await ws.send(json.dumps({"type": "playback_end", "response": 1, "e2e_ms": None}))
        await send_all(ws, utterance())
        return await recv(ws)

    first_after = with_server(scenario)
    assert first_after["type"] == "vad"
    assert first_after["audio_ms"] == (45 + 45 + 5) * FRAME_MS


def test_transcricao_vazia_nao_gera_resposta():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        msgs = await recv_until(ws, "transcript")
        await send_all(ws, utterance())  # continua ouvindo
        msgs.append(await recv(ws))
        return msgs

    msgs = with_server(scenario, transcript="")
    assert kinds(msgs) == ["vad:start", "vad:end", "transcript", "vad:start"]


def test_volta_a_ouvir_se_playback_end_nao_chega():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "response_end")
        await asyncio.sleep(0.6)  # áudio de ~0,23 s + prazo de 0,2 s
        await send_all(ws, utterance())
        return await recv(ws)

    first_after = with_server(scenario, grace=0.2)
    assert first_after["type"] == "vad"
    assert first_after["event"] == "start"


# --- Frase incompleta (pausa no meio) ---------------------------------------------------

def test_frase_incompleta_e_juntada_com_a_continuacao():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())  # "qual o tamanho da"
        await send_all(ws, utterance())  # "tablespace users?" logo em seguida
        return await recv_until(ws, "response_end")

    msgs = with_server(scenario, transcriber=SequenceTranscriber(["agatha, qual o tamanho da", "tablespace users?"]))
    assert [m["text"] for m in msgs if isinstance(m, dict) and m["type"] == "transcript"] == [
        "agatha, qual o tamanho da", "tablespace users?"
    ]
    starts = [m for m in msgs if isinstance(m, dict) and m["type"] == "response_start"]
    assert starts == [{"type": "response_start", "response": 1, "segment": 2}]
    assert FakeConversation.heard == ["agatha, qual o tamanho da tablespace users?"]


def test_frase_incompleta_sem_continuacao_responde_apos_o_prazo():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "transcript")
        t0 = time.monotonic()
        await recv_until(ws, "response_start")
        return time.monotonic() - t0

    waited = with_server(scenario, transcriber=SequenceTranscriber(["agatha, me fala sobre o"]), continuation_ms=300)
    assert 0.25 <= waited < 2
    assert FakeConversation.heard == ["agatha, me fala sobre o"]


def test_continuacao_vazia_responde_ao_que_tinha():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await send_all(ws, utterance())  # ruído que vira transcrição vazia
        return await recv_until(ws, "response_end")

    with_server(scenario, transcriber=SequenceTranscriber(["agatha, me fala sobre o", ""]), continuation_ms=5000)
    assert FakeConversation.heard == ["agatha, me fala sobre o"]


def test_frase_completa_nao_espera():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "transcript")
        t0 = time.monotonic()
        await recv_until(ws, "response_start")
        return time.monotonic() - t0

    waited = with_server(scenario, transcriber=SequenceTranscriber(["agatha, que horas são?"]), continuation_ms=5000)
    assert waited < 0.5


# --- Wake word (ADR-008) ----------------------------------------------------------------

def playback_end(n: int) -> str:
    return json.dumps({"type": "playback_end", "response": n, "e2e_ms": None})


def test_fala_sem_o_nome_e_ignorada():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())  # "que horas são?" (não chamou)
        await send_all(ws, utterance())  # "agatha, que horas são?"
        return await recv_until(ws, "response_end")

    msgs = with_server(scenario, transcriber=SequenceTranscriber(["que horas são?", "agatha, que horas são?"]))
    transcripts = [m for m in msgs if isinstance(m, dict) and m["type"] == "transcript"]
    assert [t["addressed"] for t in transcripts] == [False, True]
    assert FakeConversation.heard == ["agatha, que horas são?"]


def test_modo_conversa_aceita_fala_sem_o_nome():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "response_end")
        await ws.send(playback_end(1))
        await send_all(ws, utterance())  # logo depois, sem o nome
        return await recv_until(ws, "response_end")

    with_server(scenario, transcriber=SequenceTranscriber(["agatha, o que é o zabbix?", "e um item?"]))
    assert FakeConversation.heard == ["agatha, o que é o zabbix?", "e um item?"]


def test_modo_conversa_expira():
    async def scenario(ws):
        await start(ws)
        await send_all(ws, utterance())
        await recv_until(ws, "response_end")
        await ws.send(playback_end(1))
        # janela de 0,2 s já passou; a pausa cobre também os 640 ms de "fala" do áudio enviado de uma
        # vez (o servidor estima o início da fala como fim − duração)
        await asyncio.sleep(1.2)
        await send_all(ws, utterance())  # sem o nome: ignorada
        await send_all(ws, utterance())  # com o nome
        return await recv_until(ws, "response_end")

    with_server(scenario, conversation_s=0.2,
                transcriber=SequenceTranscriber(["agatha, oi", "e o zabbix?", "agatha, e o zabbix?"]))
    assert FakeConversation.heard == ["agatha, oi", "agatha, e o zabbix?"]
