import json

from conftest import wav_bytes, wait_for

AUDIO = {"file": ("a.wav", wav_bytes(seconds=2), "audio/wav")}


async def _poll(client, predicate, description: str, timeout: float = 30.0) -> None:
    step = 0.2
    while timeout > 0:
        if predicate((await client.get("/api/status")).json()):
            return
        timeout -= step
        await _sleep(step)
    raise AssertionError(f"never reached: {description}")


async def wait_status(client, expected: str, timeout: float = 30.0) -> None:
    await _poll(client, lambda body: body["status"] == expected, f"status={expected}", timeout)


async def wait_engine(client, variant: str, timeout: float = 30.0) -> None:
    await _poll(
        client,
        lambda body: body["status"] == "ready" and body["engine"]["variant"] == variant,
        f"engine={variant}",
        timeout,
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def test_status_shape_when_stopped(client_stopped):
    body = (await client_stopped.get("/api/status")).json()
    assert body["status"] == "stopped"
    assert body["engine"]["variant"] is None
    assert body["engine"]["running"] is False
    assert body["metrics"]["count"] == 0
    assert body["api_key_set"] is False
    assert body["glossary_count"] == 0
    assert body["defaults"]["variant"] == "rnnt"
    assert body["max_upload_mb"] == 150


async def test_status_shape_when_ready(client_ready):
    body = (await client_ready.get("/api/status")).json()
    assert body["status"] == "ready"
    assert body["engine"]["variant"] == "rnnt"
    assert body["engine"]["running"] is True


async def test_models_marks_downloaded_and_deployed(client_ready):
    heads = (await client_ready.get("/api/models")).json()["heads"]
    assert len(heads) == 4
    rnnt = next(head for head in heads if head["id"] == "rnnt")
    assert rnnt["downloaded"] is True
    assert rnnt["deployed"] is True
    assert rnnt["native_punctuation"] is False
    e2e = next(head for head in heads if head["id"] == "e2e_rnnt")
    assert e2e["downloaded"] is False and e2e["deployed"] is False


async def test_deploy_switches_head(client_ready):
    response = await client_ready.post("/api/deploy", json={"variant": "e2e_rnnt"})
    assert response.status_code == 202
    assert response.json()["config"]["variant"] == "e2e_rnnt"
    await wait_engine(client_ready, "e2e_rnnt")


async def test_deploy_accepts_option_overrides(client_stopped):
    response = await client_stopped.post(
        "/api/deploy",
        json={"variant": "rnnt", "punctuation": "on", "vad": True, "pool_size": 2},
    )
    config = response.json()["config"]
    assert config["punctuation"] == "on" and config["vad"] is True and config["pool_size"] == 2
    await wait_status(client_stopped, "ready")


async def test_deploy_rejects_unknown_variant(client_ready):
    response = await client_ready.post("/api/deploy", json={"variant": "whisper-large"})
    assert response.status_code == 422


async def test_deploy_rejects_absurd_pool_size(client_ready):
    response = await client_ready.post("/api/deploy", json={"variant": "rnnt", "pool_size": 99})
    assert response.status_code == 422


async def test_stop_then_status_stopped(client_ready):
    assert (await client_ready.post("/api/stop")).status_code == 200
    body = (await client_ready.get("/api/status")).json()
    assert body["status"] == "stopped"
    assert body["engine"]["running"] is False


async def test_glossary_get_post_roundtrip(client_ready):
    response = await client_ready.post("/api/glossary", json={"text": "АйМоп, GigaAM"})
    assert response.status_code == 200
    assert response.json()["count"] == 2
    body = (await client_ready.get("/api/glossary")).json()
    assert body["text"] == "АйМоп\nGigaAM"
    assert body["count"] == 2


async def test_events_stream_starts_with_snapshot_then_pushes_updates(live_client):
    from console.state import EngineConfig

    supervisor = live_client.app.state.supervisor
    await supervisor.deploy(EngineConfig())

    async with live_client.stream("GET", "/api/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.aiter_lines()
        payload = json.loads(await _first_data_line(lines))
        assert payload["type"] == "snapshot"
        assert payload["status"] == "ready"

        await live_client.post("/api/stop")
        statuses = []
        for _ in range(20):
            event = json.loads(await _first_data_line(lines))
            if event.get("type") == "status":
                statuses.append(event["status"])
                if "stopped" in statuses:
                    break
        assert "stopped" in statuses


async def _first_data_line(lines) -> str:
    async for line in lines:
        if line.startswith("data: "):
            return line[len("data: ") :]
    raise AssertionError("stream ended without data")


async def test_test_endpoint_returns_text_and_timing(client_ready):
    body = (await client_ready.post("/api/test", files=AUDIO)).json()
    assert body["text"] == "привет мир"
    assert body["elapsed"] >= 0
    assert body["audio_seconds"] == 2.0
    assert body["rtf"] is not None


async def test_test_endpoint_requires_ready_engine(client_stopped):
    response = await client_stopped.post("/api/test", files=AUDIO)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_not_ready"


async def test_test_endpoint_records_metrics(client_ready):
    await client_ready.post("/api/test", files=AUDIO)
    assert (await client_ready.get("/api/status")).json()["metrics"]["count"] == 1


async def test_status_carries_the_all_time_totals_and_recent_files(client_ready):
    await client_ready.post("/api/test", files=AUDIO)
    await client_ready.post(
        "/api/test", files={"file": ("вторая.wav", wav_bytes(seconds=4), "audio/wav")}
    )
    metrics = (await client_ready.get("/api/status")).json()["metrics"]
    assert metrics["total_files"] == 2
    assert metrics["total_audio_seconds"] == 6.0
    assert metrics["avg_elapsed_total"] is not None
    assert [item["name"] for item in metrics["recent"]] == ["вторая.wav", "a.wav"]


async def test_totals_outlive_the_console_process(console_settings):
    """Счётчик за всё время лежит в data/metrics.json, а не только в памяти."""
    from conftest import _client
    from console.state import EngineConfig

    first = await _client(console_settings)
    await first.app.state.supervisor.deploy(EngineConfig())
    await first.post("/api/test", files=AUDIO)
    await first.app.state.supervisor.shutdown()
    await first.aclose()

    second = await _client(console_settings)
    try:
        metrics = (await second.get("/api/status")).json()["metrics"]
        assert metrics["total_files"] == 1
        assert metrics["count"] == 0  # окно и список последних — только текущий запуск
    finally:
        await second.app.state.supervisor.shutdown()
        await second.aclose()


async def test_health_reports_engine_state_but_stays_200(client_stopped):
    response = await client_stopped.get("/health")
    assert response.status_code == 200
    assert response.json()["engine_status"] == "stopped"
    assert response.json()["status"] == "ok"


async def test_deploy_progress_visible_in_status(client_stopped, monkeypatch):
    monkeypatch.setenv("FAKE_DOWNLOAD_SLOW", "0.15")
    await client_stopped.post("/api/deploy", json={"variant": "rnnt"})
    seen: list[object] = []

    async def saw_progress() -> bool:
        body = (await client_stopped.get("/api/status")).json()
        seen.append(body["download_percent"])
        return body["status"] == "downloading" and body["download_percent"] is not None

    for _ in range(60):
        if await saw_progress():
            break
        await _sleep(0.1)
    assert any(value is not None for value in seen)
    await wait_status(client_stopped, "ready", timeout=60)


async def test_schema_reports_the_package_version(client_ready):
    """Схема обязана называть ту же версию, что и пакет: расхождение — это баг."""
    import console

    schema = (await client_ready.get("/api/openapi.json")).json()
    assert schema["info"]["version"] == console.__version__
