"""Mensagens do protocolo cliente ↔ servidor, versão 1 (ver docs/protocol.md)."""

import hmac
import json

import numpy as np

from .vad import SpeechEnd, SpeechStart, VADConfig

PROTOCOL_VERSION = 1
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}

# Códigos de fechamento WebSocket (RFC 6455)
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_POLICY_VIOLATION = 1008


class ProtocolError(Exception):
    """Erro a ser enviado ao cliente como mensagem `error`.

    `close_code` None significa que a conexão continua aberta.
    """

    def __init__(self, code: str, message: str, close_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.close_code = close_code


def parse_message(raw: str) -> dict:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        raise ProtocolError("bad_message", "JSON inválido")
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("bad_message", "mensagem sem campo 'type'")
    return msg


def parse_hello(raw: str | bytes, token: str) -> dict:
    """Valida o `hello`; qualquer problema aqui encerra a conexão."""
    if isinstance(raw, bytes):
        raise ProtocolError("audio_before_ready", "áudio enviado antes do handshake", CLOSE_POLICY_VIOLATION)
    try:
        msg = parse_message(raw)
    except ProtocolError as e:
        raise ProtocolError(e.code, e.message, CLOSE_PROTOCOL_ERROR)
    if msg["type"] != "hello":
        raise ProtocolError("bad_message", "a primeira mensagem deve ser 'hello'", CLOSE_PROTOCOL_ERROR)
    if msg.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_protocol",
            f"versão {msg.get('protocol')!r} não suportada (suportada: {PROTOCOL_VERSION})",
            CLOSE_PROTOCOL_ERROR,
        )
    if msg.get("audio") != AUDIO_FORMAT:
        raise ProtocolError(
            "unsupported_audio", f"formato de áudio aceito: {AUDIO_FORMAT}", CLOSE_UNSUPPORTED_DATA
        )
    given = msg.get("token")
    if not isinstance(given, str) or not hmac.compare_digest(given.encode(), token.encode()):
        raise ProtocolError("unauthorized", "token ausente ou inválido", CLOSE_POLICY_VIOLATION)
    return msg


def pcm16_to_float32(data: bytes) -> np.ndarray:
    if len(data) % 2:
        raise ProtocolError("bad_audio", "frame de áudio com número ímpar de bytes")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _dump(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)


def ready(session: str, vad: VADConfig) -> str:
    return _dump({
        "type": "ready",
        "protocol": PROTOCOL_VERSION,
        "session": session,
        "vad": {
            "threshold": vad.threshold,
            "min_silence_ms": vad.min_silence_ms,
            "pre_roll_ms": vad.pre_roll_ms,
            "min_speech_ms": vad.min_speech_ms,
            "max_segment_s": vad.max_segment_s,
        },
    })


def vad_event(event: SpeechStart | SpeechEnd) -> str:
    if isinstance(event, SpeechStart):
        return _dump({"type": "vad", "event": "start", "segment": event.segment, "audio_ms": event.audio_ms})
    return _dump({
        "type": "vad",
        "event": "end",
        "segment": event.segment,
        "audio_ms": event.audio_ms,
        "reason": event.reason,
    })


def transcript(segment: int, text: str, timings: dict) -> str:
    return _dump({"type": "transcript", "segment": segment, "text": text, "timings": timings})


def error(code: str, message: str) -> str:
    return _dump({"type": "error", "code": code, "message": message})
