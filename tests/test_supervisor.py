import httpx
import pytest
from conftest import wait_for

from console.state import EngineConfig, State, StateFile
from console.supervisor import Supervisor


def cfg(**kw) -> EngineConfig:
    return EngineConfig(**kw)


async def reload_count(settings) -> int:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{settings.engine_base_url}/debug/reloads")
    return response.json()["count"]


@pytest.fixture
async def supervisor(console_settings, bus):
    sup = Supervisor(console_settings, bus)
    try:
        yield sup
    finally:
        await sup.shutdown()


async def test_deploy_downloads_starts_and_reaches_ready(supervisor, console_settings):
    await supervisor.deploy(cfg())
    assert supervisor.status == "ready"
    assert (console_settings.model_dir / "v3_rnnt_encoder_int8.onnx").exists()
    assert supervisor.current.variant == "rnnt"
    stored = StateFile(console_settings.state_path).load()
    assert stored.status == "ready"
    assert stored.last_good.variant == "rnnt"


async def test_deploy_publishes_status_events(supervisor, bus):
    stream = bus.subscribe()
    await supervisor.deploy(cfg())
    seen = []
    while True:
        try:
            seen.append(await anext(stream))
        except StopAsyncIteration:  # pragma: no cover
            break
        if seen[-1].get("status") == "ready":
            break
    statuses = [event.get("status") for event in seen if event.get("type") == "status"]
    assert "downloading" in statuses and "starting" in statuses and statuses[-1] == "ready"


async def test_hotwords_seeded_from_initial_context(console_settings, bus):
    settings = console_settings.model_copy(update={"initial_context": "АйМоп, GigaAM"})
    sup = Supervisor(settings, bus)
    try:
        await sup.deploy(cfg())
        assert settings.hotwords_path.read_text(encoding="utf-8") == "АйМоп\nGigaAM\n"
        assert sup.glossary_count == 2
    finally:
        await sup.shutdown()


async def test_download_failure_keeps_previous_engine_running(
    supervisor, console_settings, monkeypatch
):
    await supervisor.deploy(cfg())
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "network")
    await supervisor.deploy(cfg(variant="e2e_rnnt"))
    assert supervisor.status == "error"
    assert "сет" in supervisor.detail.lower()
    health = await supervisor.health()
    assert health["variant"] == "rnnt"  # old engine untouched


async def test_download_retried_after_transient_network_failures(supervisor, monkeypatch):
    monkeypatch.setattr("console.supervisor.DOWNLOAD_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL_TIMES", "2")
    await supervisor.deploy(cfg())
    assert supervisor.status == "ready"


async def test_download_gives_up_after_all_retries(supervisor, monkeypatch):
    monkeypatch.setattr("console.supervisor.DOWNLOAD_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL_TIMES", "9")
    await supervisor.deploy(cfg())
    assert supervisor.status == "error"
    assert "Попыток было 3" in supervisor.detail


async def test_failed_start_rolls_back_to_last_good(supervisor, monkeypatch):
    await supervisor.deploy(cfg())
    monkeypatch.setenv("FAKE_UNHEALTHY_VARIANT", "e2e_rnnt")
    await supervisor.deploy(cfg(variant="e2e_rnnt"), startup_timeout=3)
    assert supervisor.status == "ready"
    assert supervisor.current.variant == "rnnt"
    assert "откат" in supervisor.detail.lower()


async def test_failed_first_deploy_without_last_good_ends_in_error(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_UNHEALTHY", "1")
    await supervisor.deploy(cfg(), startup_timeout=2)
    assert supervisor.status == "error"
    assert supervisor.current is None


async def test_watchdog_restarts_crashed_engine(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "1")
    await supervisor.deploy(cfg())
    monkeypatch.delenv("FAKE_CRASH_AFTER")
    await wait_for(lambda: supervisor.restart_count >= 1, timeout=15)
    await wait_for(lambda: supervisor.status == "ready", timeout=30)
    assert supervisor.current.variant == "rnnt"


async def test_restore_on_boot_redeploys_last_desired(console_settings, bus):
    StateFile(console_settings.state_path).save(
        State(
            status="ready",
            detail="",
            desired=cfg(variant="e2e_rnnt"),
            last_good=cfg(variant="e2e_rnnt"),
        )
    )
    sup = Supervisor(console_settings, bus)
    try:
        await sup.restore_on_boot()
        assert sup.status == "ready"
        assert sup.current.variant == "e2e_rnnt"
    finally:
        await sup.shutdown()


async def test_restore_on_boot_does_nothing_on_first_run(supervisor):
    await supervisor.restore_on_boot()
    assert supervisor.status == "stopped"
    assert supervisor.current is None


async def test_autostart_disabled_leaves_engine_stopped(console_settings, bus):
    StateFile(console_settings.state_path).save(
        State(status="ready", detail="", desired=cfg(), last_good=cfg())
    )
    settings = console_settings.model_copy(update={"autostart": False})
    sup = Supervisor(settings, bus)
    try:
        await sup.restore_on_boot()
        assert sup.status == "stopped"
    finally:
        await sup.shutdown()


async def test_apply_glossary_reloads_without_restart(supervisor, console_settings):
    await supervisor.deploy(cfg())
    pid = supervisor.process.pid
    assert await supervisor.apply_glossary("АйМоп, GigaAM") is True
    assert console_settings.hotwords_path.read_text(encoding="utf-8") == "АйМоп\nGigaAM\n"
    assert await reload_count(console_settings) == 1
    assert supervisor.process.pid == pid
    assert supervisor.glossary_count == 2


async def test_apply_glossary_without_engine_only_writes_file(supervisor, console_settings):
    assert await supervisor.apply_glossary("АйМоп") is True
    assert console_settings.hotwords_path.read_text(encoding="utf-8") == "АйМоп\n"
    assert supervisor.status == "stopped"


async def test_stop_engine_persists_stopped_status(supervisor, console_settings):
    await supervisor.deploy(cfg())
    await supervisor.stop_engine()
    assert supervisor.status == "stopped"
    assert supervisor.process.is_running is False
    assert StateFile(console_settings.state_path).load().status == "stopped"


async def test_shutdown_does_not_trigger_watchdog(supervisor):
    await supervisor.deploy(cfg())
    await supervisor.shutdown()
    assert supervisor.restart_count == 0


def env_settings(console_settings, **overrides):
    """Settings, где явно заданы только `overrides`: остальное - умолчания."""
    return console_settings.__class__(
        _env_file=None,
        engine_bin=console_settings.engine_bin,
        engine_port=console_settings.engine_port,
        model_dir=console_settings.model_dir,
        data_dir=console_settings.data_dir,
        **overrides,
    )


async def test_env_divergence_lists_explicit_fields_that_differ(console_settings, bus):
    settings = env_settings(console_settings, hotwords_default=True)
    supervisor = Supervisor(settings, bus)
    supervisor._desired = EngineConfig(hotwords_default=False)
    assert supervisor.env_divergence() == {"hotwords_default": {"env": True, "state": False}}


async def test_env_divergence_is_empty_when_nothing_is_explicit(console_settings, bus):
    supervisor = Supervisor(console_settings, bus)
    supervisor._desired = EngineConfig(pool_size=4)
    assert supervisor.env_divergence() == {}


async def test_env_divergence_stays_quiet_when_values_agree(console_settings, bus):
    settings = env_settings(console_settings, pool_size=4)
    supervisor = Supervisor(settings, bus)
    supervisor._desired = EngineConfig(pool_size=4)
    assert supervisor.env_divergence() == {}


async def test_initial_context_missing_ignores_case_and_yo(console_settings, bus):
    from console.glossary import write_hotwords

    console_settings.initial_context = "Пётр, GigaAM, новая"
    write_hotwords(console_settings.hotwords_path, "петр, gigaam")
    supervisor = Supervisor(console_settings, bus)
    assert supervisor.initial_context_missing() == ["новая"]


async def test_boot_warns_about_divergence_in_the_log(console_settings, bus):
    """Молчаливое расхождение - это и был дефект: .env говорил одно, разворачивалось
    другое, и узнать об этом было негде."""
    settings = env_settings(console_settings, hotwords_default=True, autostart=False)
    supervisor = Supervisor(settings, bus)
    supervisor._desired = EngineConfig(hotwords_default=False)
    await supervisor.restore_on_boot()
    assert any("ВНИМАНИЕ" in line and "hotwords_default" in line for line in bus.log_lines())
    await supervisor.shutdown()


async def test_deploying_names_the_target_and_clears_after_ready(
    console_settings, bus, monkeypatch
):
    """Интерфейс рисует прогресс на карточке цели, поэтому цель обязана быть названа.

    Во время отката цель - прежняя голова, а не та, что не поднялась: иначе полоса
    висела бы на карточке, которую уже никто не разворачивает.
    """
    supervisor = Supervisor(console_settings, bus)
    try:
        await supervisor.deploy(cfg())
        assert supervisor.deploying is None

        stream = bus.subscribe()
        monkeypatch.setenv("FAKE_UNHEALTHY_VARIANT", "e2e_rnnt")
        await supervisor.deploy(cfg(variant="e2e_rnnt"), startup_timeout=3)
        assert supervisor.status == "ready"
        assert supervisor.deploying is None

        seen = []
        while True:
            try:
                seen.append(await anext(stream))
            except StopAsyncIteration:  # pragma: no cover
                break
            if seen[-1].get("status") == "ready":
                break

        starting = [
            event
            for event in seen
            if event.get("type") == "status" and event.get("status") == "starting"
        ]
        rollback = [e for e in starting if "откат" in (e.get("detail") or "").lower()]
        forward = [e for e in starting if "откат" not in (e.get("detail") or "").lower()]
        assert forward and all(event["variant"] == "e2e_rnnt" for event in forward)
        assert rollback and all(event["variant"] == "rnnt" for event in rollback)
    finally:
        await supervisor.shutdown()
