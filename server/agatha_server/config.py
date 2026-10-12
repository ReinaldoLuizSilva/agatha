"""Configuração do servidor via variáveis de ambiente (prefixo AGATHA_) e arquivo .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from .conversation import ConversationConfig
from .llm import LLMConfig
from .memory import MemoryConfig
from .tools.oracle import OracleConfig
from .tools.zabbix import ZabbixConfig
from .stt import STTConfig
from .tts import TTSConfig
from .vad import VADConfig
from .wake import WakeConfig


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    token: str
    log_level: str
    stt: STTConfig
    vad: VADConfig
    llm: LLMConfig
    tts: TTSConfig
    conversation: ConversationConfig
    wake: WakeConfig = WakeConfig()
    memory: MemoryConfig = MemoryConfig()
    zabbix: ZabbixConfig = ZabbixConfig()
    oracle: OracleConfig = OracleConfig()


def load_env_file(path: Path, env: dict) -> None:
    """Lê KEY=VALUE de um .env sem sobrescrever variáveis já definidas."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(env: dict | None = None) -> ServerConfig:
    env = dict(os.environ if env is None else env)
    load_env_file(Path(env.get("AGATHA_ENV_FILE", ".env")), env)

    def get(name: str, default, cast=str):
        value = env.get(f"AGATHA_{name}")
        if value in (None, ""):
            return default
        try:
            return cast(value)
        except ValueError:
            raise ConfigError(f"AGATHA_{name}={value!r} inválido")

    token = get("TOKEN", "")
    if not token:
        raise ConfigError(
            "AGATHA_TOKEN não definido. Gere um com:\n"
            "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'\n"
            "e coloque no .env (veja .env.example)."
        )

    stt_defaults = STTConfig()
    vad_defaults = VADConfig()
    llm_defaults = LLMConfig()
    tts_defaults = TTSConfig()
    conv_defaults = ConversationConfig()
    wake_defaults = WakeConfig()
    memory_defaults = MemoryConfig()

    def flag(name: str, default: bool) -> bool:
        value = env.get(f"AGATHA_{name}")
        if value in (None, ""):
            return default
        if value.lower() in ("1", "true", "sim", "yes", "on"):
            return True
        if value.lower() in ("0", "false", "nao", "não", "no", "off"):
            return False
        raise ConfigError(f"AGATHA_{name}={value!r} inválido (use true/false)")

    words = get("WAKE_WORDS", None)
    return ServerConfig(
        host=get("HOST", "0.0.0.0"),
        port=get("PORT", 8765, int),
        token=token,
        log_level=get("LOG_LEVEL", "INFO").upper(),
        stt=STTConfig(
            model=get("STT_MODEL", stt_defaults.model),
            device=get("STT_DEVICE", stt_defaults.device),
            compute_type=get("STT_COMPUTE_TYPE", stt_defaults.compute_type),
            beam_size=get("STT_BEAM_SIZE", stt_defaults.beam_size, int),
            language=get("STT_LANGUAGE", stt_defaults.language),
            models_dir=get("STT_MODELS_DIR", stt_defaults.models_dir),
            cpu_threads=get("STT_CPU_THREADS", stt_defaults.cpu_threads, int),
            initial_prompt=get("STT_INITIAL_PROMPT", stt_defaults.initial_prompt),
        ),
        vad=VADConfig(
            threshold=get("VAD_THRESHOLD", vad_defaults.threshold, float),
            min_silence_ms=get("VAD_MIN_SILENCE_MS", vad_defaults.min_silence_ms, int),
            pre_roll_ms=get("VAD_PRE_ROLL_MS", vad_defaults.pre_roll_ms, int),
            min_speech_ms=get("VAD_MIN_SPEECH_MS", vad_defaults.min_speech_ms, int),
            max_segment_s=get("VAD_MAX_SEGMENT_S", vad_defaults.max_segment_s, float),
            continuation_ms=get("VAD_CONTINUATION_MS", vad_defaults.continuation_ms, int),
        ),
        llm=LLMConfig(
            url=get("LLM_URL", llm_defaults.url),
            temperature=get("LLM_TEMPERATURE", llm_defaults.temperature, float),
            max_tokens=get("LLM_MAX_TOKENS", llm_defaults.max_tokens, int),
        ),
        tts=TTSConfig(
            voice=get("TTS_VOICE", tts_defaults.voice),
            length_scale=get("TTS_LENGTH_SCALE", tts_defaults.length_scale, float),
            threads=get("TTS_THREADS", tts_defaults.threads, int),
        ),
        conversation=ConversationConfig(
            history_turns=get("HISTORY_TURNS", conv_defaults.history_turns, int),
        ),
        wake=WakeConfig(
            enabled=flag("REQUIRE_WAKE_WORD", wake_defaults.enabled),
            words=tuple(w.strip() for w in words.split(",") if w.strip()) if words else wake_defaults.words,
            conversation_s=get("CONVERSATION_S", wake_defaults.conversation_s, float),
        ),
        memory=MemoryConfig(
            db=get("MEMORY_DB", memory_defaults.db),
            max_facts=get("MEMORY_MAX_FACTS", memory_defaults.max_facts, int),
        ),
        zabbix=ZabbixConfig(url=get("ZABBIX_URL", ""), token=get("ZABBIX_TOKEN", "")),
        oracle=OracleConfig(dsn=get("ORACLE_DSN", ""), user=get("ORACLE_USER", ""),
                            password=get("ORACLE_PASSWORD", "")),
    )
