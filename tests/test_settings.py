import pytest
from pydantic import ValidationError

from console.settings import Settings


def test_defaults_are_dictation_friendly():
    s = Settings(_env_file=None)
    assert s.console_port == 8080
    assert s.model_variant == "rnnt"
    assert s.pool_size == 1  # all cores to one request => lowest latency
    assert s.autostart is True
    assert s.engine_base_url == "http://127.0.0.1:9876"
    assert s.api_key == ""
    # Встроенный словарь брендов включен по умолчанию: замер 04.09.2026 на 27 минутах
    # настоящей диктовки показал одно изменение на 2654 слова, и оно в сторону
    # правильного написания (см. docs/research/head-choice-and-wer.md).
    assert s.hotwords_default is True


def test_env_overrides_and_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_VARIANT", "e2e_rnnt")
    monkeypatch.setenv("POOL_SIZE", "2")
    monkeypatch.setenv("VAD", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    assert s.model_variant == "e2e_rnnt"
    assert s.pool_size == 2
    assert s.vad is True
    assert s.state_path == tmp_path / "state.json"
    assert s.hotwords_path == tmp_path / "hotwords.txt"


def test_invalid_variant_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_variant="whisper-large")


def test_invalid_punctuation_mode_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, punctuation="yes-please")


def test_initial_context_and_hotwords_knobs(monkeypatch):
    monkeypatch.setenv("INITIAL_CONTEXT", "АйМоп, GigaAM")
    monkeypatch.setenv("HOTWORDS_BOOST", "7.5")
    monkeypatch.setenv("HOTWORDS_DEFAULT", "1")
    s = Settings(_env_file=None)
    assert s.initial_context == "АйМоп, GigaAM"
    assert s.hotwords_boost == 7.5
    assert s.hotwords_default is True


def test_explicit_engine_fields_names_only_what_was_set(monkeypatch):
    """Расхождение с state.json имеет смысл показывать только по тем полям, которые
    пользователь задал сам: про значение по умолчанию никто ничего не просил."""
    monkeypatch.setenv("HOTWORDS_DEFAULT", "1")
    monkeypatch.setenv("MODEL_VARIANT", "e2e_rnnt")
    s = Settings(_env_file=None)
    assert s.explicit_engine_fields() == {"hotwords_default", "variant"}
    cfg = s.engine_config_from_env()
    assert cfg.variant == "e2e_rnnt" and cfg.hotwords_default is True and cfg.pool_size == 1


def test_engine_config_from_env_ignores_console_only_knobs():
    """Порт и ключ - не аргументы движка, и в расхождении им делать нечего."""
    s = Settings(_env_file=None, console_port=9999, api_key="x")
    assert s.explicit_engine_fields() == set()
    assert s.engine_config_from_env() == Settings(_env_file=None).engine_config_from_env()
