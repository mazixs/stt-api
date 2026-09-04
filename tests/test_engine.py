import asyncio

import pytest

from console.engine import EngineProcess, EngineStartupError, build_argv
from console.state import EngineConfig

from conftest import wait_for


def cfg(**kw) -> EngineConfig:
    return EngineConfig(**kw)


def test_argv_maps_config_to_engine_flags(console_settings):
    config = EngineConfig(
        variant="e2e_rnnt",
        punctuation="on",
        itn="off",
        vad=True,
        pool_size=2,
        hotwords_boost=7.5,
        hotwords_default=True,
    )
    argv = build_argv(console_settings, config)
    assert "serve" in argv[:3]  # `.py` doubles are prefixed with the interpreter
    assert argv[argv.index("--model-variant") + 1] == "e2e_rnnt"
    assert argv[argv.index("--punctuation") + 1] == "on"
    assert argv[argv.index("--itn") + 1] == "off"
    assert "--vad" in argv
    assert argv[argv.index("--pool-size") + 1] == "2"
    assert argv[argv.index("--hotwords-boost") + 1] == "7.5"
    assert "--hotwords-default" in argv
    assert "--bind-all" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == str(console_settings.engine_port)
    assert argv[argv.index("--model-dir") + 1] == str(console_settings.model_dir)
    assert argv[argv.index("--hotwords-file") + 1] == str(console_settings.hotwords_path)
    # The engine's own body limit must sit above ours, or a file that just fits
    # MAX_UPLOAD_MB dies inside the engine's multipart parser instead of being
    # refused by the console with a message about size.
    limit = int(argv[argv.index("--body-limit-bytes") + 1])
    assert limit > console_settings.max_upload_bytes
    # Side models must live in the mounted volume, not in the engine user's home.
    assert argv[argv.index("--punct-model-dir") + 1] == str(console_settings.model_dir / "punct")
    assert argv[argv.index("--vad-model-dir") + 1] == str(console_settings.model_dir / "vad")


def test_optional_flags_omitted_when_off(console_settings):
    argv = build_argv(console_settings, cfg(hotwords_default=False))
    assert "--vad" not in argv
    assert "--hotwords-default" not in argv
    assert "--enable-jobs" not in argv


def test_brand_lexicon_flag_present_by_default(console_settings):
    """Умолчание изменилось на включенный словарь, и это должно доходить до argv,
    а не только до .env."""
    assert "--hotwords-default" in build_argv(console_settings, cfg())


def test_enable_jobs_flag_follows_settings(console_settings):
    settings = console_settings.model_copy(update={"enable_jobs": True})
    assert "--enable-jobs" in build_argv(settings, cfg())


async def test_start_then_healthy_then_stop(console_settings):
    proc = EngineProcess(console_settings)
    await proc.start(cfg())
    health = await proc.wait_healthy(timeout=20)
    assert health["variant"] == "rnnt"
    assert proc.is_running is True
    await proc.stop()
    assert proc.is_running is False


async def test_health_returns_none_when_not_running(console_settings):
    proc = EngineProcess(console_settings)
    assert await proc.health() is None


async def test_wait_healthy_times_out_when_model_stays_loading(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_UNHEALTHY", "1")
    proc = EngineProcess(console_settings)
    await proc.start(cfg())
    with pytest.raises(EngineStartupError):
        await proc.wait_healthy(timeout=2)
    await proc.stop()


async def test_wait_healthy_fails_fast_when_process_dies(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "0.2")
    monkeypatch.setenv("FAKE_STARTUP_DELAY", "5")
    proc = EngineProcess(console_settings)
    await proc.start(cfg())
    with pytest.raises(EngineStartupError):
        await proc.wait_healthy(timeout=15)


async def test_exit_callback_fires_on_crash(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "0.5")
    codes: list[int] = []
    proc = EngineProcess(console_settings, on_exit=codes.append)
    await proc.start(cfg())
    await wait_for(lambda: bool(codes), timeout=10)
    assert codes[0] != 0


async def test_stop_does_not_fire_exit_callback(console_settings):
    codes: list[int] = []
    proc = EngineProcess(console_settings, on_exit=codes.append)
    await proc.start(cfg())
    await proc.wait_healthy(timeout=20)
    await proc.stop()
    await asyncio.sleep(0.3)
    assert codes == []


async def test_logs_forwarded_to_callback(console_settings):
    lines: list[str] = []
    proc = EngineProcess(console_settings, on_log=lines.append)
    await proc.start(cfg())
    await proc.wait_healthy(timeout=20)
    await wait_for(lambda: any("listening" in line for line in lines), timeout=10)
    await proc.stop()


async def test_reload_calls_engine_admin_endpoint(console_settings):
    proc = EngineProcess(console_settings)
    await proc.start(cfg())
    await proc.wait_healthy(timeout=20)
    assert await proc.reload() is True
    await proc.stop()


def test_body_limit_follows_max_upload(console_settings):
    """Лимит движка считается от нашего, а не остаётся значением по умолчанию."""
    small = console_settings.model_copy(update={"max_upload_mb": 5})
    big = console_settings.model_copy(update={"max_upload_mb": 500})
    for settings in (small, big):
        argv = build_argv(settings, EngineConfig())
        limit = int(argv[argv.index("--body-limit-bytes") + 1])
        assert limit == settings.max_upload_bytes + 1024 * 1024
        # 500 МБ — это выше собственных 50 МиБ движка: без флага такая настройка
        # молча не работала, хотя README предлагает её поднимать.
        assert limit > 50 * 1024 * 1024 or settings.max_upload_mb < 50
