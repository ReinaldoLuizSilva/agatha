import pytest

from agatha_server.config import ConfigError, load_config


def test_token_obrigatorio(tmp_path):
    with pytest.raises(ConfigError, match="AGATHA_TOKEN"):
        load_config({"AGATHA_ENV_FILE": str(tmp_path / "nao-existe")})


def test_padroes(tmp_path):
    config = load_config({"AGATHA_TOKEN": "x", "AGATHA_ENV_FILE": str(tmp_path / "nao-existe")})
    assert (config.host, config.port) == ("0.0.0.0", 8765)
    assert (config.stt.model, config.stt.device, config.stt.compute_type) == ("small", "cuda", "int8")
    assert config.vad.min_silence_ms == 500


def test_env_file_nao_sobrescreve_ambiente(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comentário\n"
        "AGATHA_TOKEN='do-arquivo'\n"
        "AGATHA_PORT=9000\n"
        "AGATHA_VAD_MIN_SILENCE_MS=400\n"
    )
    config = load_config({"AGATHA_ENV_FILE": str(env_file), "AGATHA_PORT": "9100"})
    assert config.token == "do-arquivo"
    assert config.port == 9100  # o ambiente tem prioridade
    assert config.vad.min_silence_ms == 400


def test_valor_invalido(tmp_path):
    with pytest.raises(ConfigError, match="AGATHA_PORT"):
        load_config({"AGATHA_TOKEN": "x", "AGATHA_PORT": "abc", "AGATHA_ENV_FILE": str(tmp_path / "x")})


def test_wake_word(tmp_path):
    base = {"AGATHA_TOKEN": "x", "AGATHA_ENV_FILE": str(tmp_path / "x")}
    config = load_config(base)
    assert config.wake.enabled and "agatha" in config.wake.words and config.wake.conversation_s == 20
    config = load_config({**base, "AGATHA_REQUIRE_WAKE_WORD": "false", "AGATHA_WAKE_WORDS": "jarvis, sexta",
                          "AGATHA_CONVERSATION_S": "5"})
    assert (config.wake.enabled, config.wake.words, config.wake.conversation_s) == (False, ("jarvis", "sexta"), 5.0)
    with pytest.raises(ConfigError, match="REQUIRE_WAKE_WORD"):
        load_config({**base, "AGATHA_REQUIRE_WAKE_WORD": "talvez"})


def test_memoria(tmp_path):
    base = {"AGATHA_TOKEN": "x", "AGATHA_ENV_FILE": str(tmp_path / "x")}
    assert load_config(base).memory.db == "/dados/agatha/data/memory/agatha.db"
    config = load_config({**base, "AGATHA_MEMORY_DB": "/tmp/m.db", "AGATHA_MEMORY_MAX_FACTS": "10"})
    assert (config.memory.db, config.memory.max_facts) == ("/tmp/m.db", 10)
