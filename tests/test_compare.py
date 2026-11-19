import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from console import compare
from console.catalog import HEADS
from console.main import create_app
from console.state import EngineConfig
from conftest import wait_for, wav_bytes

KEY = "comparison-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
FILE = "private-filename.wav"
AUDIO = wav_bytes(0.5)


def enable(settings):
    settings.enable_model_compare = True
    settings.api_key = KEY
    return settings


def install(settings, *variants):
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    for variant in variants:
        for name in HEADS[variant].files:
            (settings.model_dir / name).write_bytes(b"existing weights")


async def submit(client, *, a="rnnt", b="e2e_rnnt", audio=AUDIO, headers=AUTH):
    return await client.post(
        "/api/compare", headers=headers, data={"variant_a": a, "variant_b": b},
        files={"file": (FILE, audio, "audio/wav")},
    )


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert KEY not in response.text and FILE not in response.text


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    record_path = tmp_path / "cli.jsonl"
    env = compare._cli_env()
    env["FAKE_COMPARE_RECORD"] = str(record_path)
    monkeypatch.setattr(compare, "_cli_env", lambda: dict(env))

    def records():
        return [json.loads(line) for line in record_path.read_text().splitlines()] if record_path.exists() else []

    return env, records


@pytest.fixture
def children(monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append((process, args))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    return processes


async def test_compare_disabled_by_default_and_requires_api_key(client_stopped, monkeypatch):
    client = client_stopped
    settings = client.app.state.settings
    assert (await client.get("/api/status")).json()["compare_enabled"] is False
    error(await submit(client), 503, "compare_disabled")

    monkeypatch.setenv("ENABLE_MODEL_COMPARE", "1")
    assert type(settings)(_env_file=None).enable_model_compare is True
    settings.enable_model_compare = True
    assert (await client.get("/api/status")).json()["compare_enabled"] is False
    error(await submit(client), 503, "compare_disabled")

    settings.api_key = KEY
    assert (await client.get("/api/status", headers=AUTH)).json()["compare_enabled"] is True
    # При отказе даже чтение аудио для запуска двух CLI еще не началось.
    async def unexpected_read(*args, **kwargs):
        pytest.fail("Сравнение открыло файл до проверки API-ключа")

    monkeypatch.setattr(compare, "_read_audio", unexpected_read)
    error(await submit(client, headers={}), 401, "invalid_api_key")
    error(await submit(client, headers={"Authorization": "Bearer wrong"}), 401, "invalid_api_key")


async def test_compare_runs_two_isolated_cold_processes_in_order(client_stopped, fake_cli):
    client = client_stopped
    settings = enable(client.app.state.settings)
    supervisor = client.app.state.supervisor
    await supervisor.deploy(EngineConfig())
    install(settings, "e2e_rnnt")
    pid = supervisor.process.pid
    stored = settings.state_path.read_bytes()
    models = {p.name: p.read_bytes() for p in settings.model_dir.iterdir()}
    env, records = fake_cli
    env["FAKE_COMPARE_DELAY"] = "0.05"

    response = await submit(client, a="e2e_rnnt", b="rnnt")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"results", "audio_seconds"}
    assert payload["audio_seconds"] == 0.5
    for result, variant in zip(payload["results"], ("e2e_rnnt", "rnnt"), strict=True):
        assert set(result) == {"variant", "text", "elapsed"}
        assert result["variant"] == variant
        assert result["text"] == f"привет от {variant}"
        assert result["elapsed"] >= 0.05

    entries = records()
    assert [(d["phase"], d["variant"]) for d in entries] == [
        ("start", "e2e_rnnt"), ("done", "e2e_rnnt"), ("start", "rnnt"), ("done", "rnnt"),
    ]
    assert {entry["sha256"] for entry in entries} == {hashlib.sha256(AUDIO).hexdigest()}
    assert len({entry["audio"] for entry in entries}) == 1
    assert len({entry["model_dir"] for entry in entries}) == 2
    assert all(not Path(entry["audio"]).exists() for entry in entries)
    assert all(not Path(entry["model_dir"]).exists() for entry in entries)
    assert all(FILE not in entry["audio"] for entry in entries)
    assert supervisor.status == "ready" and supervisor.current.variant == "rnnt"
    assert supervisor.process.pid == pid and settings.state_path.read_bytes() == stored
    assert {p.name: p.read_bytes() for p in settings.model_dir.iterdir()} == models


@pytest.mark.parametrize(("a", "b", "audio", "code"), [
    ("", "rnnt", AUDIO, "compare_missing_variant"),
    ("secret-variant", "rnnt", AUDIO, "compare_unknown_variant"),
    ("rnnt", "rnnt", AUDIO, "compare_same_variant"),
    ("rnnt", "e2e_rnnt", b"", "compare_empty_file"),
], ids=("missing", "unknown", "same", "empty"))
async def test_compare_validates_request(client_stopped, children, a, b, audio, code):
    enable(client_stopped.app.state.settings)
    response = await submit(client_stopped, a=a, b=b, audio=audio)
    error(response, 400, code)
    assert "secret-variant" not in response.text and children == []


async def test_compare_does_not_read_large_upload_into_memory(client_stopped, fake_cli, monkeypatch):
    settings = enable(client_stopped.app.state.settings)
    install(settings, "rnnt", "e2e_rnnt")
    read = compare.UploadFile.read

    async def bounded_read(self, size=-1):
        assert size > 0, "Сравнение не должно читать весь файл в память"
        return await read(self, size)

    monkeypatch.setattr(compare.UploadFile, "read", bounded_read)
    audio = wav_bytes(64)
    response = await submit(client_stopped, audio=audio)
    assert response.status_code == 200 and response.json()["audio_seconds"] == 64
    assert {event["sha256"] for event in fake_cli[1]()} == {hashlib.sha256(audio).hexdigest()}


async def test_compare_never_downloads_missing_head(client_stopped, children):
    settings = enable(client_stopped.app.state.settings)
    install(settings, "rnnt")
    response = await submit(client_stopped)
    error(response, 409, "compare_model_not_downloaded")
    assert children == []
    assert not (settings.model_dir / HEADS["e2e_rnnt"].files[0]).exists()


async def test_compare_reuses_limit_and_limits_chunked_multipart(client_stopped, children):
    settings = enable(client_stopped.app.state.settings)
    settings.max_upload_mb = 1
    error(
        await submit(client_stopped, audio=b"0" * (settings.max_upload_bytes + 1)),
        413, "file_too_large",
    )

    async def chunks():
        yield b'--cut\r\nContent-Disposition: form-data; name="file"; filename="private.wav"\r\n\r\n'
        chunk = b"0" * (512 * 1024)
        for _ in range(3):
            yield chunk
        pytest.fail("Тело сверх лимита не должно быть дочитано.")

    response = await client_stopped.post("/api/compare", content=chunks(), headers={
        **AUTH, "Content-Type": "multipart/form-data; boundary=cut",
    })
    error(response, 413, "file_too_large")
    assert children == []


async def test_compare_sanitizes_malformed_multipart(client_stopped, children):
    enable(client_stopped.app.state.settings)
    response = await client_stopped.post(
        "/api/compare", content=b"--wrong", headers={
            **AUTH, "Content-Type": "multipart/form-data; boundary=expected",
        },
    )
    error(response, 400, "invalid_multipart")
    assert children == []


async def test_compare_sanitizes_cli_error_and_keeps_no_partial_results(client_stopped, fake_cli):
    settings = enable(client_stopped.app.state.settings)
    install(settings, "rnnt", "e2e_rnnt")
    env, records = fake_cli
    env["FAKE_COMPARE_FAIL"] = "e2e_rnnt"
    secret = f"{KEY} {FILE} private-audio"
    env["FAKE_COMPARE_ERROR"] = secret
    response = await submit(client_stopped)
    error(response, 502, "compare_engine_error")
    assert "results" not in response.json() and "private-audio" not in response.text
    assert all(not Path(entry["audio"]).exists() for entry in records())


async def test_compare_timeout_reaps_cli_and_releases_slot(client_stopped, fake_cli, children, monkeypatch):
    settings = enable(client_stopped.app.state.settings)
    install(settings, "rnnt", "e2e_rnnt")
    env, _ = fake_cli
    env["FAKE_COMPARE_DELAY"] = "30"
    monkeypatch.setattr(compare, "COMPARE_TIMEOUT", 2.0)

    response = await submit(client_stopped)
    error(response, 504, "compare_timeout")
    (process, args), = children
    assert process.returncode is not None
    assert not Path(args[args.index("transcribe") + 1]).exists()
    assert not client_stopped.app.state.compare_lock.locked()

    monkeypatch.setattr(compare, "COMPARE_TIMEOUT", 10.0)
    del env["FAKE_COMPARE_DELAY"]
    assert (await submit(client_stopped)).status_code == 200


async def test_live_compare_rejects_busy_and_kills_on_disconnect(live_client, fake_cli, children):
    settings = enable(live_client.app.state.settings)
    install(settings, "rnnt", "e2e_rnnt")
    env, records = fake_cli
    env["FAKE_COMPARE_DELAY"] = "30"
    request = asyncio.create_task(submit(live_client))
    try:
        await wait_for(lambda: bool(records()))
        error(await submit(live_client), 409, "compare_busy")
    finally:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    await wait_for(lambda: not live_client.app.state.compare_lock.locked())
    assert len(children) == 1 and children[0][0].returncode is not None
    assert not Path(records()[0]["audio"]).exists()

    del env["FAKE_COMPARE_DELAY"]
    assert (await submit(live_client)).status_code == 200


async def test_cancellation_while_starting_cli_reaps_it(client_stopped, fake_cli, children, monkeypatch):
    settings = enable(client_stopped.app.state.settings)
    install(settings, "rnnt", "e2e_rnnt")
    fake_cli[0]["FAKE_COMPARE_DELAY"] = "30"
    entering_spawn, release_spawn = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        entering_spawn.set()
        await release_spawn.wait()
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    request = asyncio.create_task(submit(client_stopped))
    try:
        await asyncio.wait_for(entering_spawn.wait(), timeout=5)
        request.cancel()
        await asyncio.sleep(0)
        assert client_stopped.app.state.compare_lock.locked()
    finally:
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert len(children) == 1 and children[0][0].returncode is not None
    assert not client_stopped.app.state.compare_lock.locked()


@pytest.mark.parametrize("operation", ("deploy", "watchdog"))
async def test_engine_start_preempts_compare_and_waits_for_reap(
    client_stopped, fake_cli, children, monkeypatch, operation,
):
    client = client_stopped
    settings = enable(client.app.state.settings)
    supervisor = client.app.state.supervisor
    await supervisor.deploy(EngineConfig())
    install(settings, "e2e_rnnt")
    children.clear()
    env, records = fake_cli
    env["FAKE_COMPARE_DELAY"] = "30"
    comparison = asyncio.create_task(submit(client))
    deployment = None
    reached_start = asyncio.Event()
    allow_start = asyncio.Event()
    start = supervisor.process.start
    stop = supervisor.process.stop

    def assert_reaped():
        assert children[0][0].returncode is not None
        assert not Path(records()[0]["audio"]).exists()

    async def blocked_start(cfg):
        assert_reaped()
        reached_start.set()
        await allow_start.wait()
        await start(cfg)

    async def guarded_stop():
        assert_reaped()
        await stop()

    try:
        await wait_for(lambda: bool(records()))
        ready_request = await client.post(
            "/v1/audio/transcriptions", headers=AUTH, files={"file": (FILE, AUDIO)},
        )
        assert ready_request.status_code == 200
        monkeypatch.setattr(supervisor.process, "start", blocked_start)
        if operation == "deploy":
            monkeypatch.setattr(supervisor.process, "stop", guarded_stop)
            deployment = asyncio.create_task(supervisor.deploy(EngineConfig(variant="e2e_rnnt")))
        else:
            supervisor.process._process.kill()
        await asyncio.wait_for(reached_start.wait(), timeout=15)
        error(await comparison, 409, "compare_preempted")
        error(await submit(client), 409, "compare_engine_busy")
        assert [(entry["phase"], entry["variant"]) for entry in records()] == [("start", "rnnt")]
    finally:
        allow_start.set()
        if deployment is not None:
            await deployment
        if not comparison.done():
            comparison.cancel()
            await asyncio.gather(comparison, return_exceptions=True)

    await wait_for(lambda: supervisor.status == "ready")
    del env["FAKE_COMPARE_DELAY"]
    assert (await submit(client)).status_code == 200


async def test_glossary_reload_keeps_gate_if_request_is_cancelled(client_ready, monkeypatch):
    supervisor = client_ready.app.state.supervisor
    enable(client_ready.app.state.settings)
    rebuilding, release = asyncio.Event(), asyncio.Event()

    async def reload():
        rebuilding.set()
        await release.wait()
        return True

    monkeypatch.setattr(supervisor.process, "reload", reload)
    request = asyncio.create_task(client_ready.post("/api/glossary", headers=AUTH, json={"text": "тест"}))
    try:
        await asyncio.wait_for(rebuilding.wait(), 5)
        request.cancel()
        await asyncio.sleep(0)
        error(await submit(client_ready), 409, "compare_engine_busy")
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    assert supervisor.comparison_allowed


async def test_stop_interrupts_native_reload_before_releasing_gate(client_stopped, tmp_path, monkeypatch):
    client = client_stopped
    settings = client.app.state.settings
    supervisor = client.app.state.supervisor
    release = tmp_path / "release-reload"
    monkeypatch.setenv("FAKE_RELOAD_RELEASE", str(release))
    await supervisor.deploy(EngineConfig())
    reload_request = asyncio.create_task(client.post("/v1/admin/reload"))
    try:
        async with httpx.AsyncClient(base_url=settings.engine_base_url, trust_env=False) as direct:
            async with asyncio.timeout(5):
                while not (await direct.get("/debug/reloads")).json()["count"]:
                    await asyncio.sleep(0.02)
        response = await asyncio.wait_for(client.post("/api/stop"), 5)
        assert response.status_code == 200 and supervisor.status == "stopped"
        assert not supervisor.process.is_running and supervisor.comparison_allowed
        error(await reload_request, 503, "engine_not_ready")
        assert supervisor.journal.snapshot()[0]["params"]["variant"] == "rnnt"
    finally:
        release.touch()
        await asyncio.gather(reload_request, return_exceptions=True)


async def test_native_reload_holds_gate_after_client_cancellation(
    client_stopped, fake_cli, children, tmp_path, monkeypatch,
):
    client = client_stopped
    settings = enable(client.app.state.settings)
    supervisor = client.app.state.supervisor
    release = tmp_path / "release-native-reload"
    monkeypatch.setenv("FAKE_RELOAD_RELEASE", str(release))
    await supervisor.deploy(EngineConfig())
    install(settings, "e2e_rnnt")
    children.clear()
    env, records = fake_cli
    env["FAKE_COMPARE_DELAY"] = "30"
    url = "/v1/admin/reload?soft=true"
    headers = {**AUTH, "x-reload-test": "preserved"}
    payload = b"raw body"
    assert (await client.post(url)).status_code == 401
    cleaning, release_cleanup = asyncio.Event(), asyncio.Event()
    kill_and_reap = compare._kill_and_reap

    async def hold_cleanup(spawn):
        cleaning.set()
        await release_cleanup.wait()
        await kill_and_reap(spawn)

    monkeypatch.setattr(compare, "_kill_and_reap", hold_cleanup)
    comparison = asyncio.create_task(submit(client))
    reload_request = None
    try:
        await wait_for(lambda: bool(records()))
        reload_request = asyncio.create_task(client.post(url, headers=headers, content=payload))
        async with httpx.AsyncClient(base_url=settings.engine_base_url) as direct:
            await asyncio.wait_for(cleaning.wait(), timeout=10)
            assert (await direct.get("/debug/reloads")).json()["count"] == 0
            release_cleanup.set()
            async with asyncio.timeout(10):
                while not (await direct.get("/debug/reloads")).json()["count"]:
                    await asyncio.sleep(0.05)

        error(await comparison, 409, "compare_preempted")
        assert children[0][0].returncode is not None
        assert not Path(records()[0]["audio"]).exists()
        reload_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_request
        error(await submit(client), 409, "compare_engine_busy")
        duplicate = await client.post(url, headers=headers, content=payload)
        assert duplicate.status_code == 409
        assert duplicate.json() == {
            "error": "A model reload is already in progress", "code": "reload_in_progress",
        }
        response = await client.post(
            "/v1/audio/transcriptions", headers=AUTH, files={"file": (FILE, AUDIO)},
        )
        assert response.status_code == 200
    finally:
        release_cleanup.set()
        release.touch()
        for task in (comparison, reload_request):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    await wait_for(lambda: not supervisor._lock.locked())
    response = await client.post(url, headers=headers, content=payload)
    assert response.status_code == 202
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"path": url, "body": "raw body", "header": "preserved"}
    del env["FAKE_COMPARE_DELAY"]
    assert (await submit(client)).status_code == 200


async def test_compare_rechecks_gate_if_deploy_starts_during_upload(client_stopped, children, monkeypatch):
    client = client_stopped
    supervisor = client.app.state.supervisor
    enable(client.app.state.settings)
    receiving, finish_upload = asyncio.Event(), asyncio.Event()
    downloading, finish_download = asyncio.Event(), asyncio.Event()
    read_audio = compare._read_audio

    async def slow_upload(*args):
        receiving.set()
        await finish_upload.wait()
        return await read_audio(*args)

    async def download_in_progress(*_args):
        downloading.set()
        await finish_download.wait()
        return False

    monkeypatch.setattr(compare, "_read_audio", slow_upload)
    monkeypatch.setattr(supervisor, "_download_with_retries", download_in_progress)
    comparison = asyncio.create_task(submit(client))
    await asyncio.wait_for(receiving.wait(), timeout=5)
    deployment = asyncio.create_task(supervisor.deploy(EngineConfig()))
    try:
        await asyncio.wait_for(downloading.wait(), timeout=5)
        finish_upload.set()
        error(await comparison, 409, "compare_engine_busy")
        assert children == []
    finally:
        finish_upload.set()
        finish_download.set()
        await asyncio.gather(comparison, deployment, return_exceptions=True)


async def test_startup_removes_only_its_stale_compare_directory(console_settings, tmp_path, monkeypatch):
    settings = enable(console_settings)
    settings.autostart = False
    monkeypatch.setattr(compare.tempfile, "gettempdir", lambda: str(tmp_path))
    root = compare._temp_root(settings)
    root.mkdir(mode=0o700)
    leftovers = root / "aborted-job"
    leftovers.mkdir()
    (leftovers / "audio").write_bytes(AUDIO)
    other = tmp_path / "stt-api-compare-other"
    other.mkdir()
    neighbor = other / "keep"
    neighbor.write_bytes(b"other console")
    if os.name != "nt":
        (leftovers / "model-link").symlink_to(other, target_is_directory=True)

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert not root.exists()
        assert neighbor.read_bytes() == b"other console"
        assert settings.compare_enabled is True


def test_compare_cli_environment_does_not_copy_secrets_or_server_options(monkeypatch):
    for name in ("API_KEY", "HF_TOKEN", "GIGASTT_HOTWORDS_FILE", "GIGASTT_VAD", "GIGASTT_FORMAT"):
        monkeypatch.setenv(name, "private")
    assert "private" not in compare._cli_env().values()
