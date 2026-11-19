import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import _client, wait_for, wav_bytes

from console import supervisor as supervisor_module
from console.catalog import is_downloaded
from console.state import EngineConfig


async def _events(client, **kwargs):
    response = await client.get("/api/journal", **kwargs)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"events"}
    return body["events"]


def _check_operation(events, expected):
    """Сверяет полную цепочку одной операции в порядке появления."""
    assert len(events) == len(expected)
    operation_id = events[0]["params"]["operation_id"]
    assert isinstance(operation_id, str) and operation_id
    for event, (category, level, code, params) in zip(reversed(events), expected, strict=True):
        assert set(event) == {"id", "at", "category", "level", "code", "params"}
        assert event["category"] == category
        assert event["level"] == level
        assert event["code"] == code
        assert event["params"] == {"operation_id": operation_id, **params}
        assert event["at"].endswith("Z") and datetime.fromisoformat(event["at"]).tzinfo == UTC
    assert len({event["id"] for event in events}) == len(events)
    return operation_id


_RNNT_DEPLOY = [
    ("deploy", "info", "deploy.downloading", {"variant": "rnnt"}),
    ("deploy", "info", "deploy.starting", {"variant": "rnnt"}),
    ("engine", "info", "deploy.ready", {"variant": "rnnt"}),
]


async def test_journal_deploy_records_ready_only_after_health(client_stopped, monkeypatch):
    supervisor = client_stopped.app.state.supervisor
    downloaded, allow_start = asyncio.Event(), asyncio.Event()
    checking_health, allow_health = asyncio.Event(), asyncio.Event()
    download = supervisor_module.download
    wait_healthy = supervisor.process.wait_healthy

    async def hold_after_download(*args):
        await download(*args)
        downloaded.set()
        await allow_start.wait()

    async def hold_before_health(*, timeout):
        checking_health.set()
        await allow_health.wait()
        return await wait_healthy(timeout=timeout)

    monkeypatch.setattr(supervisor_module, "download", hold_after_download)
    monkeypatch.setattr(supervisor.process, "wait_healthy", hold_before_health)
    assert await _events(client_stopped) == []
    response = await client_stopped.post("/api/deploy", json={"variant": "rnnt"})
    task = client_stopped.app.state.deploy_task
    try:
        assert response.status_code == 202
        assert all(event["code"] != "deploy.ready" for event in await _events(client_stopped))
        await asyncio.wait_for(downloaded.wait(), timeout=10)
        status = (await client_stopped.get("/api/status")).json()
        assert status["download_percent"] == 100 and status["status"] == "downloading"
        _check_operation(await _events(client_stopped), _RNNT_DEPLOY[:1])
        allow_start.set()
        await asyncio.wait_for(checking_health.wait(), timeout=10)
        _check_operation(await _events(client_stopped), _RNNT_DEPLOY[:2])
    finally:
        allow_start.set()
        allow_health.set()
        await task
    first = await _events(client_stopped)
    first_id = _check_operation(first, _RNNT_DEPLOY)
    assert await _events(client_stopped) == first

    assert is_downloaded("rnnt", supervisor.settings.model_dir)
    await supervisor.deploy(EngineConfig())
    all_events = await _events(client_stopped)
    assert _check_operation(all_events[:3], _RNNT_DEPLOY) != first_id
    assert all_events[3:] == first


async def test_journal_download_retry_then_ready(client_stopped, monkeypatch):
    monkeypatch.setattr(supervisor_module, "DOWNLOAD_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL_TIMES", "2")
    await client_stopped.app.state.supervisor.deploy(EngineConfig())
    _check_operation(
        await _events(client_stopped),
        [
            _RNNT_DEPLOY[0],
            *[
                ("deploy", "warning", "download.retry", {
                    "variant": "rnnt", "kind": "network", "attempt": attempt, "attempts": 3,
                })
                for attempt in (2, 3)
            ],
            *_RNNT_DEPLOY[1:],
        ],
    )


@pytest.mark.parametrize(("kind", "attempts"), [("checksum", 3), ("disk", 1)])
async def test_journal_download_failure_does_not_promise_old_model_readiness(
    client_ready, monkeypatch, kind, attempts
):
    monkeypatch.setattr(supervisor_module, "DOWNLOAD_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", kind)
    before = await _events(client_ready)
    await client_ready.app.state.supervisor.deploy(EngineConfig(variant="ml_ctc"))
    events = await _events(client_ready)
    _check_operation(
        events[:-len(before)],
        [
            ("deploy", "info", "deploy.downloading", {"variant": "ml_ctc"}),
            *[
                ("deploy", "warning", "download.retry", {
                    "variant": "ml_ctc", "kind": kind, "attempt": attempt, "attempts": attempts,
                })
                for attempt in range(2, attempts + 1)
            ],
            ("deploy", "error", "download.failed", {
                "variant": "ml_ctc", "kind": kind, "attempts": attempts, "running": "rnnt",
            }),
        ],
    )
    assert events[-len(before):] == before
    status = (await client_ready.get("/api/status")).json()
    assert status["status"] == "error" and status["engine"]["running"]
    response = await client_ready.post("/api/test")
    assert response.status_code == 503 and response.json()["error"]["code"] == "engine_not_ready"


async def test_journal_model_dir_error_is_terminal_and_safe(console_settings):
    marker = "MODEL-DIR-MARKER"
    bad_dir = console_settings.model_dir.with_name(marker)
    bad_dir.touch()
    settings = console_settings.model_copy(update={"model_dir": bad_dir})
    async with await _client(settings) as client:
        supervisor = client.app.state.supervisor
        try:
            await supervisor.deploy(EngineConfig())
            failure = {"variant": "rnnt", "kind": "disk", "attempts": 1}
            _check_operation(
                await _events(client),
                [_RNNT_DEPLOY[0], ("deploy", "error", "download.failed", failure)],
            )
            status = (await client.get("/api/status")).json()
            assert status["status"] == "error" and status["detail_code"] == "download.failed"
            assert marker not in status["detail"]
            assert marker not in (await client.get("/api/journal")).text
        finally:
            await supervisor.shutdown()


@pytest.mark.parametrize("rollback_ok", [True, False])
async def test_journal_rollback_names_both_heads(client_ready, monkeypatch, rollback_ok):
    monkeypatch.setenv("FAKE_UNHEALTHY_VARIANT", "ml_ctc")
    if not rollback_ok:
        monkeypatch.setenv("FAKE_UNHEALTHY", "1")
    supervisor = client_ready.app.state.supervisor
    await supervisor.deploy(EngineConfig(variant="ml_ctc"), startup_timeout=2)
    events = await _events(client_ready)
    assert len(events) == 8
    target = {"variant": "ml_ctc"}
    rollback = {"variant": "rnnt", "failed": "ml_ctc"}
    code, level = ("rollback.done", "info") if rollback_ok else ("rollback.failed", "error")
    _check_operation(
        events[:5],
        [
            ("deploy", "info", "deploy.downloading", target),
            ("deploy", "info", "deploy.starting", target),
            ("deploy", "error", "start.failed", target),
            ("deploy", "warning", "rollback.starting", rollback),
            ("deploy", level, code, rollback),
        ],
    )
    assert supervisor.status == ("ready" if rollback_ok else "error")
    assert (supervisor.current.variant if supervisor.current else None) == (
        "rnnt" if rollback_ok else None
    )


@pytest.mark.parametrize("health_returned", [False, True])
async def test_journal_early_exit_rolls_back_without_watchdog(
    client_ready, monkeypatch, health_returned
):
    supervisor = client_ready.app.state.supervisor
    early_exit = asyncio.Event()
    wait_healthy = supervisor.process.wait_healthy
    on_exit = supervisor.process.on_exit

    def observe_exit(code):
        on_exit(code)
        early_exit.set()

    async def check_health_after_exit(*, timeout):
        if supervisor.process.config.variant == "ml_ctc":
            await asyncio.wait_for(early_exit.wait(), timeout=timeout)
            monkeypatch.delenv("FAKE_CRASH_AFTER")
            if health_returned:
                # Имитация успешного ответа, уже полученного к моменту выхода процесса.
                return {"status": "ok", "model": "gigaam-v3-ml_ctc"}
        return await wait_healthy(timeout=timeout)

    monkeypatch.setenv("FAKE_CRASH_AFTER", "0.01")
    monkeypatch.setattr(supervisor.process, "on_exit", observe_exit)
    monkeypatch.setattr(supervisor.process, "wait_healthy", check_health_after_exit)
    await supervisor.deploy(EngineConfig(variant="ml_ctc"), startup_timeout=15)

    events = await _events(client_ready)
    target = {"variant": "ml_ctc"}
    rollback = {"variant": "rnnt", "failed": "ml_ctc"}
    _check_operation(
        events[:-3],
        [
            ("deploy", "info", "deploy.downloading", target),
            ("deploy", "info", "deploy.starting", target),
            ("deploy", "error", "start.failed", target),
            ("deploy", "warning", "rollback.starting", rollback),
            ("deploy", "info", "rollback.done", rollback),
        ],
    )
    assert supervisor.restart_count == 0 and supervisor._restart_task is None
    assert supervisor.status == "ready" and supervisor.current.variant == "rnnt"
    assert any("engine exited unexpectedly" in line for line in supervisor.bus.log_lines())


async def test_journal_explicit_stop_and_history_is_not_persisted(client_ready, console_settings):
    response = await client_ready.post("/api/stop")
    assert response.status_code == 200
    events = await _events(client_ready)
    stop_id = _check_operation(events[:1], [("engine", "info", "idle.stopped", {"variant": "rnnt"})])
    assert stop_id != _check_operation(events[1:], _RNNT_DEPLOY)

    async with await _client(console_settings) as restarted:
        assert await _events(restarted) == []
        assert (await restarted.get("/api/status")).json()["defaults"]["variant"] == "rnnt"


async def test_journal_watchdog_failed_attempt_then_recovery(client_ready, monkeypatch):
    supervisor = client_ready.app.state.supervisor
    monkeypatch.setattr(supervisor_module, "BACKOFF_DELAYS", (0.0,))
    monkeypatch.setenv("FAKE_UNHEALTHY", "1")
    wait_healthy = supervisor.process.wait_healthy
    first_attempt = True

    async def unhealthy_once(*, timeout):
        nonlocal first_attempt
        if not first_attempt:
            return await wait_healthy(timeout=timeout)
        first_attempt = False
        try:
            return await wait_healthy(timeout=2.0)
        finally:
            monkeypatch.delenv("FAKE_UNHEALTHY")

    monkeypatch.setattr(supervisor.process, "wait_healthy", unhealthy_once)
    supervisor.process._process.kill()
    await wait_for(lambda: supervisor.detail_code == "watchdog.ready", timeout=60)
    events = await _events(client_ready)
    assert len(events) == 8
    variant = {"variant": "rnnt"}
    first, second = dict(variant, attempt=1), dict(variant, attempt=2)
    recovery_id = _check_operation(
        events[:5],
        [
            ("engine", "warning", "watchdog.exited", variant),
            ("engine", "warning", "watchdog.restarting", first),
            ("engine", "error", "watchdog.failed", first),
            ("engine", "warning", "watchdog.restarting", second),
            ("engine", "info", "watchdog.ready", second),
        ],
    )
    assert recovery_id != _check_operation(events[5:], _RNNT_DEPLOY)


async def test_stop_during_watchdog_delay_cancels_recovery(client_ready, monkeypatch):
    supervisor = client_ready.app.state.supervisor
    monkeypatch.setattr(supervisor_module, "BACKOFF_DELAYS", (60.0,))
    supervisor.process._process.kill()
    await wait_for(lambda: supervisor.detail_code == "watchdog.exited")
    recovery = supervisor._restart_task
    response = await client_ready.post("/api/stop")
    assert response.status_code == 200 and recovery.done()
    assert supervisor.comparison_allowed and supervisor.status == "stopped"
    assert (await _events(client_ready))[0]["code"] == "idle.stopped"


async def test_watchdog_recovers_running_head_after_failed_download(client_ready, monkeypatch):
    supervisor = client_ready.app.state.supervisor
    monkeypatch.setattr(supervisor_module, "BACKOFF_DELAYS", (0.0,))
    download = supervisor_module.download

    async def download_with_broken_ml_ctc(settings, variant, on_event):
        with monkeypatch.context() as patch:
            if variant == "ml_ctc":
                patch.setenv("FAKE_DOWNLOAD_FAIL", "disk")
            return await download(settings, variant, on_event)

    monkeypatch.setattr(supervisor_module, "download", download_with_broken_ml_ctc)
    await supervisor.deploy(EngineConfig(variant="ml_ctc"))
    assert supervisor.detail_code == "download.failed"
    assert supervisor.current.variant == "rnnt" and supervisor.desired.variant == "ml_ctc"
    supervisor.process._process.kill()
    await wait_for(lambda: supervisor.detail_code == "watchdog.ready", timeout=15)
    assert supervisor.current.variant == "rnnt"
    assert (await _events(client_ready))[0]["params"]["variant"] == "rnnt"

    await supervisor.shutdown()
    async with await _client(supervisor.settings) as restarted:
        restored = restarted.app.state.supervisor
        try:
            await restored.restore_on_boot()
            assert restored.status == "ready" and restored.current.variant == "rnnt"
            assert (await restored.health())["model"] == "gigaam-v3-rnnt"
            _check_operation(await _events(restarted), _RNNT_DEPLOY)
        finally:
            await restored.shutdown()


async def test_journal_restricts_fields_and_does_not_evade_standard_auth(
    console_settings, monkeypatch
):
    key = "KEY-MARKER"
    hidden_kind = "https://example.test/private?token=URL-MARKER"
    monkeypatch.setenv("FAKE_TRANSCRIPT", "TRANSCRIPT-MARKER")
    monkeypatch.setenv("FAKE_ANSI", "1")
    settings = console_settings.model_copy(update={"api_key": key})
    async with await _client(settings) as client:
        supervisor = client.app.state.supervisor
        try:
            assert (await client.get("/api/journal")).status_code == 401
            client.headers["Authorization"] = f"Bearer {key}"
            await supervisor.deploy(EngineConfig())
            before = await _events(client)
            upload = {"file": ("FILENAME-MARKER.wav", wav_bytes(), "audio/wav")}
            assert (await client.post("/api/test", files=upload)).json()["text"] == "TRANSCRIPT-MARKER"

            async def reload_failed():
                return False

            monkeypatch.setattr(supervisor.process, "reload", reload_failed)
            response = await client.post("/api/glossary", json={"text": "GLOSSARY-MARKER"})
            assert response.json()["applied"]
            assert supervisor.detail_code == "deploy.ready"
            assert await _events(client) == before

            monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", hidden_kind)
            await supervisor.deploy(EngineConfig(variant="ml_ctc"))
            assert any(hidden_kind in line for line in supervisor.bus.log_lines())
            assert any("Model ready" in line for line in supervisor.bus.log_lines())
            response = await client.get("/api/journal")
            assert response.status_code == 200
            assert response.json()["events"][0]["params"]["kind"] == "other"
            for hidden in (key, hidden_kind, "FILENAME-MARKER", "TRANSCRIPT-MARKER",
                           "GLOSSARY-MARKER", "Model ready", "listening on", "v3_vocab.txt"):
                assert hidden not in response.text
        finally:
            await supervisor.shutdown()


async def test_journal_retains_only_newest_200(client_stopped):
    journal = client_stopped.app.state.supervisor.journal
    operation_id = uuid4()
    journal.record("idle.stopped", operation_id, variant="rnnt")
    oldest = (await _events(client_stopped))[0]
    journal.record("idle.stopped", operation_id, variant="e2e_rnnt")
    second = (await _events(client_stopped))[0]
    for _ in range(199):
        journal.record("idle.stopped", operation_id, variant="ml_ctc")
    events = await _events(client_stopped)
    assert len(events) == 200 and len({event["id"] for event in events}) == 200
    assert events[0]["params"]["variant"] == "ml_ctc"
    assert events[-1] == second and oldest not in events
    assert await _events(client_stopped) == events
