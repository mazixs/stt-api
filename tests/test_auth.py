import json
from urllib.parse import quote

import httpx
import pytest

from conftest import wav_bytes


KEY = "local-test-key"
BEARER = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer wrong"}
AUDIO = {"file": ("silence.wav", wav_bytes(0.1), "audio/wav")}


def assert_unauthorized(response):
    assert response.status_code == 401, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert error["type"] == "authentication_error"


@pytest.mark.parametrize("path", [
    "/v1/models", "/api/status", "/api/journal", "/api/docs", "/api/openapi.json",
])
async def test_read_routes_require_bearer_only_when_key_is_set(client_stopped, path):
    client = client_stopped
    settings = client.app.state.settings
    settings.api_key = KEY
    for headers in ({}, WRONG):
        assert_unauthorized(await client.get(path, headers=headers))
    assert (await client.get(path, headers=BEARER)).status_code == 200

    settings.api_key = ""
    assert (await client.get(path)).status_code == 200


@pytest.mark.parametrize("path", ["/v1/models", "/api/status", "/api/journal"])
async def test_client_and_management_reads_reject_query_auth(client_stopped, path):
    client_stopped.app.state.settings.api_key = KEY
    params = {"api_key": KEY}
    assert_unauthorized(await client_stopped.get(path, params=params))
    assert (await client_stopped.get(path, params=params, headers=BEARER)).status_code == 200


async def test_glossary_changes_only_after_auth(client_stopped):
    client = client_stopped
    settings = client.app.state.settings
    settings.api_key = KEY
    payload = {"text": "слово"}
    for headers, params in (({}, {}), (WRONG, {}), ({}, {"api_key": KEY})):
        assert_unauthorized(await client.post("/api/glossary", headers=headers, params=params, json=payload))
        assert not settings.hotwords_path.exists()

    response = await client.post("/api/glossary", headers=BEARER, json=payload)
    assert response.status_code == 200 and response.json()["count"] == 1
    assert (await client.get("/api/glossary", headers=BEARER)).json()["text"] == "слово"

    settings.api_key = ""
    assert (await client.post("/api/glossary", json={"text": ""})).json()["count"] == 0


@pytest.mark.parametrize(("path", "upstream_path"), [
    ("/v1/audio/transcriptions", "/v1/audio/transcriptions"),
    ("/api/test", "/v1/audio/transcriptions"),
    ("/v1/transcribe", "/v1/transcribe"),
])
async def test_upload_auth_before_engine_request(client_ready, monkeypatch, path, upstream_path):
    client = client_ready
    settings = client.app.state.settings
    settings.api_key = KEY
    calls = []
    send = httpx.AsyncHTTPTransport.handle_async_request

    async def record(transport, request):
        calls.append((request.method, str(request.url)))
        return await send(transport, request)

    # Вход в консоль идет через ASGI; этот транспорт видит только выход в движок.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record)
    for headers in ({}, WRONG):
        assert_unauthorized(await client.post(path, files=AUDIO, headers=headers))
    assert calls == []

    for key, headers in ((KEY, BEARER), ("", {})):
        settings.api_key = key
        response = await client.post(path, files=AUDIO, headers=headers)
        assert response.status_code == 200 and response.json()["text"] == "привет мир"
        assert calls == [("POST", settings.engine_base_url + upstream_path)]
        calls.clear()


async def test_translations_auth_before_unsupported_operation(client_stopped):
    client_stopped.app.state.settings.api_key = KEY
    for headers in ({}, WRONG):
        assert_unauthorized(await client_stopped.post("/v1/audio/translations", headers=headers, files=AUDIO))
    response = await client_stopped.post("/v1/audio/translations", headers=BEARER, files=AUDIO)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "translations_not_supported"


async def test_health_never_protected(client_stopped):
    client_stopped.app.state.settings.api_key = KEY
    for headers in ({}, WRONG, BEARER):
        response = await client_stopped.get("/health", headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "ok"


async def test_sse_auth_and_first_event(live_client):
    client = live_client
    settings = client.app.state.settings
    settings.api_key = KEY
    for headers, params in (({}, {}), (WRONG, {}), ({}, {"api_key": "wrong"}), (WRONG, {"api_key": KEY})):
        async with client.stream("GET", "/api/events", headers=headers, params=params) as response:
            assert response.status_code == 401  # не читаем бесконечный поток при регрессии
            await response.aread()
            assert_unauthorized(response)
    for key, headers, params in (
        (KEY, BEARER, {}), (KEY, {}, {"api_key": KEY}),
        (KEY, BEARER, {"api_key": "wrong"}), ("", {}, {}),
    ):
        settings.api_key = key
        async with client.stream("GET", "/api/events", headers=headers, params=params) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            line = await anext(response.aiter_lines())
            assert line.startswith("data: ") and json.loads(line[6:])["type"] == "snapshot"


async def test_swagger_propagates_encoded_query_key_to_schema(client_stopped):
    key = "тест +&/#"
    client_stopped.app.state.settings.api_key = key
    schema_url = f"/api/openapi.json?api_key={quote(key, safe='')}"
    response = await client_stopped.get("/api/docs", params={"api_key": key})
    assert response.status_code == 200 and schema_url in response.text
    schema = await client_stopped.get(schema_url)
    assert schema.status_code == 200 and "/api/deploy" in schema.json()["paths"]


@pytest.mark.parametrize("path", ["/api/docs", "/api/openapi.json"])
async def test_docs_prefer_bearer_to_query(client_stopped, path):
    client_stopped.app.state.settings.api_key = KEY
    for headers, query in (({}, "ошибка"), (WRONG, KEY)):
        assert_unauthorized(await client_stopped.get(path, headers=headers, params={"api_key": query}))
    response = await client_stopped.get(path, headers=BEARER, params={"api_key": "wrong"})
    assert response.status_code == 200


async def test_docs_have_no_open_default_paths(client_stopped):
    """Штатные /docs и /openapi.json выключены, иначе схема утекала бы мимо ключа."""
    client_stopped.app.state.settings.api_key = KEY
    assert (await client_stopped.get("/docs")).status_code == 404
    assert (await client_stopped.get("/openapi.json")).status_code == 404
