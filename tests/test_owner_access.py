"""Изолированный профиль владельца: граница сети, учётная запись и API."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from console import owner_access, owner_store
from console.errors import ApiError
from console.main import create_app
from console.state import EngineConfig
from conftest import wait_for, wav_bytes

ORIGIN = "https://kamerton.test"
PROXY = "192.0.2.7"
LOGIN = {"Origin": ORIGIN, "X-Kamerton-Login": "1"}
ACCOUNT = {"username": "владелец", "password": "проверочный-пароль"}


def cli(settings, *args, **extra):
    env = {k: v for k, v in os.environ.items() if k.upper() not in
           {"ADMIN_USERNAME", "ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE", "API_KEY"}}
    env.update(DATA_DIR=str(settings.data_dir), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               ADMIN_USERNAME=ACCOUNT["username"],
               ADMIN_PASSWORD=ACCOUNT["password"])
    env.update(extra)
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run([sys.executable, "-m", "console.owner_cli", *args],
                          cwd=settings.data_dir.parent, env=env, capture_output=True, timeout=20)


@pytest.fixture
def managed(console_settings):
    return console_settings.model_copy(update={"owner_access": True, "public_origin": ORIGIN,
                                                "trusted_proxy_ip": PROXY, "autostart": False})


def http(app, peer=PROXY):
    return httpx.AsyncClient(base_url=ORIGIN, headers={"X-Forwarded-Proto": "https"},
                             transport=httpx.ASGITransport(app=app, client=(peer, 12345)))


@pytest.fixture
async def owner(managed):
    assert cli(managed, "init").returncode == 0
    app = create_app(managed)
    async with app.router.lifespan_context(app), http(app) as client:
        yield client, app


async def login(client, account=ACCOUNT):
    return await client.post("/auth/login", json=account, headers=LOGIN)


async def test_network_boundary(owner):
    client, app = owner
    assert (await client.get("/auth/session")).json() == {"mode": "owner", "authenticated": False}
    for headers in ({"Host": "other.test"}, {"X-Forwarded-Proto": "http"},
                    {"X-Forwarded-Proto": "https,http"}, [("X-Forwarded-Proto", "https")] * 2,
                    [("Host", "kamerton.test")] * 2):
        assert (await client.get("/auth/session", headers=headers)).status_code == 403
    without_xfp = client.build_request("GET", "/auth/session")
    del without_xfp.headers["X-Forwarded-Proto"]
    assert (await client.send(without_xfp)).status_code == 403
    assert (await client.get("/health")).status_code == 403
    async with http(app, peer="127.0.0.1") as internal:
        assert (await internal.get("/health")).status_code == 200
        assert (await internal.get("/auth/session", headers={"X-Forwarded-For": PROXY})).status_code == 403
    assert (await client.get("/")).headers["location"] == "/login"
    assert (await client.get("/static/index.html")).status_code == 401


async def test_canonical_dns_host_and_strict_origin(managed):
    managed.public_origin = "https://KamerTON.Test:9443"
    assert cli(managed, "init").returncode == 0
    app = create_app(managed)
    async with http(app) as client:
        client.headers["Host"] = "KAMERTON.tEsT:9443"
        page = await client.get("/login")
        assert page.status_code == 200 and page.headers["referrer-policy"] == "same-origin"
        for host in ("kamerton.test", "kamerton.test:80", "kamerton.test:443", "kamerton.test.:9443"):
            assert (await client.get("/login", headers={"Host": host})).status_code == 403
        for origin in ("null", ORIGIN, "http://kamerton.test:9443", ORIGIN + ":9443/", ORIGIN + ":9443?"):
            rejected = await client.post("/auth/login", headers={**LOGIN, "Origin": origin}, json=ACCOUNT)
            assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "invalid_origin"
        valid = await client.post("/auth/login", headers={**LOGIN, "Origin": ORIGIN + ":9443"}, json=ACCOUNT)
        assert valid.status_code == 200


async def test_authenticated_json_limit_matches_glossary_limit(owner, monkeypatch):
    client, _ = owner
    monkeypatch.setattr(owner_access, "JSON_LIMIT", 100)
    assert "json_limit_bytes" not in (await client.get("/auth/session")).json()
    result = await login(client)
    session = (await client.get("/auth/session")).json()
    assert session["json_limit_bytes"] == 100 and result.json() == session
    headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"], "Content-Type": "application/json"}
    for size, expected in ((100, 200), (101, 413)):
        body = b'{"text":"' + b"a" * (size - 11) + b'"}'
        assert len(body) == size
        assert (await client.post("/api/glossary", content=body, headers=headers)).status_code == expected


async def test_activity_renews_only_idle_after_owner_authorization(owner):
    client, app = owner
    assert (await client.post("/auth/activity", headers={"Origin": ORIGIN})).status_code == 401
    csrf = (await login(client)).json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    store = app.state.owner_store
    with store.connection(write=True) as db:
        original = time.time() - owner_store.IDLE_SECONDS + 15
        created = original - 60
        db.execute("UPDATE sessions SET created=?, touched=?", (created, original))
    for bad in ({"Origin": "null", "X-CSRF-Token": csrf}, {"Origin": ORIGIN}, {"X-CSRF-Token": csrf}):
        assert (await client.post("/auth/activity", headers=bad)).status_code == 403
    assert (await client.get("/auth/activity")).status_code == 405
    assert (await client.put("/auth/activity", headers=headers)).status_code == 405
    assert (await client.post("/auth/activity", headers=headers, content=b"x")).status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT touched FROM sessions").fetchone()[0] == original
    response = await client.post("/auth/activity", headers=headers)
    assert response.status_code == 204 and not response.content
    with store.connection(write=True) as db:
        row = db.execute("SELECT created,touched FROM sessions").fetchone()
        assert row[0] == created and row[1] > original
        db.execute("UPDATE sessions SET created=?", (time.time() - owner_store.ABSOLUTE_SECONDS,))
    assert (await client.post("/auth/activity", headers=headers)).status_code == 401


async def test_cookie_and_key_are_separate(owner):
    client, app = owner
    result = await login(client)
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert all(item in cookie for item in ("__Host-kamerton_session=", "Secure", "HttpOnly", "SameSite=strict", "Path=/"))
    csrf = {"X-CSRF-Token": result.json()["csrf_token"], "Origin": ORIGIN}
    assert (await client.get("/auth/session")).json() == result.json()
    assert (await client.get("/v1/models")).status_code == 401
    assert (await client.get("/api/client-models")).json()["object"] == "list"
    for headers in ({}, {"Origin": ORIGIN}, {**csrf, "Origin": "https://other.test"}):
        assert (await client.post("/api/keys", json={"name": "a"}, headers=headers)).status_code == 403
    created = await client.post("/api/keys", json={"name": "a"}, headers=csrf)
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    key = created.json()["key"]
    assert key not in (await client.get("/api/keys")).text
    async with http(app) as machine:
        machine.headers["authorization"] = "Bearer " + key
        assert (await machine.get("/v1/models")).status_code == 200
        assert (await machine.post("/v1/audio/translations")).json()["error"]["code"] == "translations_not_supported"
        for path in ("/api/status", "/api/events", "/api/docs", "/api/openapi.json", "/api/keys"):
            assert (await machine.get(path)).status_code == 401
        assert (await machine.get("/v1/jobs")).status_code == 404
        assert (await client.post(f"/api/keys/{created.json()['id']}/revoke", headers=csrf)).status_code == 204
        assert (await machine.get("/v1/models")).status_code == 401
    assert (await client.post("/auth/logout", headers=csrf)).status_code == 204
    assert (await client.get("/api/status", headers={"Cookie": cookie.split(";")[0]})).status_code == 401


@pytest.mark.parametrize("update", [
    {"public_origin": "http://kamerton.test"}, {"public_origin": ORIGIN + "/console"},
    {"public_origin": ORIGIN + ":80"}, {"public_origin": ORIGIN + ":443"},
    {"public_origin": ORIGIN + "?"}, {"public_origin": ORIGIN + "#"}, {"public_origin": "https://a@b.test"},
    {"public_origin": ""}, {"trusted_proxy_ip": "127.0.0.1/8"}, {"trusted_proxy_ip": "*"},
    {"trusted_proxy_ip": "127.0.0.1,::1"}, {"engine_host": "0.0.0.0"}, {"engine_host": "lan"},
])
def test_invalid_network_does_not_start(managed, update):
    with pytest.raises(ValueError):
        create_app(managed.model_copy(update=update))


@pytest.mark.parametrize("bad_db", [None, b"", b"invalid-db"])
async def test_no_bootstrap_or_repair_on_web_start(managed, bad_db):
    path = managed.data_dir / "owner.sqlite3"
    if bad_db is not None:
        path.parent.mkdir()
        path.write_bytes(bad_db)
    app = create_app(managed)
    with pytest.raises(ApiError, match="Хранилище"):
        async with app.router.lifespan_context(app):
            pytest.fail("Сервис с некорректным хранилищем запустился")
    assert path.exists() == (bad_db is not None)
    if bad_db is not None:
        assert path.read_bytes() == bad_db


async def test_bootstrap_file_reset_and_one_time_legacy_import(managed, tmp_path):
    legacy_key = "synthetic-legacy-key"
    secret_file = tmp_path / "owner-secret"
    secret_file.write_bytes(ACCOUNT["password"].encode() + b"\n")
    wrong_sources = cli(managed, "init", ADMIN_PASSWORD_FILE=str(secret_file))
    assert wrong_sources.returncode != 0
    assert cli(managed, "init", ADMIN_PASSWORD=None).returncode != 0
    init = cli(managed, "init", "--import-legacy", ADMIN_PASSWORD=None,
               ADMIN_PASSWORD_FILE=str(secret_file), API_KEY=legacy_key)
    assert init.returncode == 0
    assert ACCOUNT["password"].encode() not in init.stdout + init.stderr
    assert legacy_key.encode() not in init.stdout + init.stderr
    assert cli(managed, "init", "--import-legacy", API_KEY="other").returncode != 0
    path = managed.data_dir / "owner.sqlite3"
    assert ACCOUNT["password"].encode() not in path.read_bytes()
    assert legacy_key.encode() not in path.read_bytes() and b"$argon2id$" in path.read_bytes()
    managed.api_key = legacy_key
    app = create_app(managed)
    async with http(app) as client:
        client.headers["authorization"] = "Bearer " + legacy_key
        assert (await client.get("/v1/models")).status_code == 200
        assert (await client.get("/api/status")).status_code == 401
        result = await login(client)
        csrf = {"X-CSRF-Token": result.json()["csrf_token"], "Origin": ORIGIN}
        record, = (await client.get("/api/keys")).json()["keys"]
        assert record["name"] == "legacy"
        assert (await client.post(f"/api/keys/{record['id']}/revoke", headers=csrf)).status_code == 204
        assert cli(managed, "reset-password", ADMIN_USERNAME="другой").returncode != 0
        assert (await client.get("/api/status")).status_code == 200
        assert cli(managed, "reset-password", ADMIN_PASSWORD="замена").returncode == 0
        assert (await client.get("/api/status")).status_code == 401
        assert (await login(client)).status_code == 401
        assert (await login(client, {**ACCOUNT, "password": "замена"})).status_code == 200
    restarted = create_app(managed)
    async with restarted.router.lifespan_context(restarted), http(restarted) as client:
        assert (await client.get("/v1/models", headers={"Authorization": "Bearer " + legacy_key})).status_code == 401


async def test_no_query_key_and_no_echo(owner):
    client, app = owner
    secret = "secret-that-must-not-be-echoed"
    for path in ("/api/docs", "/api/openapi.json", "/api/events", "/v1/models", "/"):
        resp = await client.get(path, params={"api_key": secret})
        assert resp.status_code == 400 and secret not in resp.text and "location" not in resp.headers
    assert (await client.post("/auth/login", json=ACCOUNT)).status_code == 403
    assert (await client.post("/auth/login", json=ACCOUNT, headers={"Origin": ORIGIN})).status_code == 403
    resp = await client.post("/auth/login", json={"password": [secret]}, headers=LOGIN)
    assert resp.status_code == 400 and secret not in resp.text
    valid = await login(client)
    csrf = {"X-CSRF-Token": valid.json()["csrf_token"], "Origin": ORIGIN}
    invalid = await client.post("/api/deploy", json={"variant": secret}, headers=csrf)
    assert invalid.status_code == 422 and secret not in invalid.text
    docs = await client.get("/api/docs")
    assert docs.status_code == 200 and "<script" not in docs.text and 'href="/api/openapi.json"' in docs.text
    assert (await client.get("/api/openapi.json")).status_code == 200
    async def chunks():
        for _ in range(3):
            yield b"x" * 4096
    resp = await client.post("/auth/login", content=chunks(), headers={**LOGIN, "Content-Type": "application/json"})
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "json_too_large"


async def test_limiter_is_per_trusted_client_and_bounded(owner):
    client, app = owner
    client.headers["X-Kamerton-Client-IP"] = "192.0.2.12"
    errors = []
    for i in range(owner_store.ATTEMPT_LIMIT):
        response = await login(client, {"username": ACCOUNT["username"] if i % 2 else "nobody", "password": "bad"})
        assert response.status_code == 401
        errors.append(response.json())
    assert all(body == errors[0] for body in errors)
    client.headers["X-Forwarded-For"] = "198.51.100.31"
    limited = await login(client)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    client.headers["X-Kamerton-Client-IP"] = "192.0.2.13"
    assert (await login(client)).status_code == 200
    slots = app.state.login_slots
    async with slots, slots:
        assert (await login(client)).status_code == 429


@pytest.mark.parametrize("expiry", ["idle", "absolute", "logout"])
async def test_sse_stops_and_never_renews(expiry, owner, monkeypatch):
    client, app = owner
    monkeypatch.setattr(owner_access, "SSE_CHECK_SECONDS", 0.02)
    valid = await login(client)
    csrf = {"Origin": ORIGIN, "X-CSRF-Token": valid.json()["csrf_token"]}
    store = app.state.owner_store
    with store.connection(write=True) as db:
        db.execute("UPDATE sessions SET touched=touched-30")
        original = db.execute("SELECT touched FROM sessions").fetchone()[0]
    stream = asyncio.create_task(client.get("/api/events"))
    try:
        await wait_for(lambda: app.state.bus.subscriber_count == 1)
        for path in ("/api/status", "/api/journal", "/api/models", "/api/glossary", "/auth/session"):
            assert (await client.get(path)).status_code == 200
        with store.connection(write=True) as db:
            assert db.execute("SELECT touched FROM sessions").fetchone()[0] == original
            if expiry == "idle":
                db.execute("UPDATE sessions SET touched=?", (time.time() - owner_store.IDLE_SECONDS,))
            if expiry == "absolute":
                db.execute("UPDATE sessions SET created=?", (time.time() - owner_store.ABSOLUTE_SECONDS,))
        if expiry == "logout":
            assert (await client.post("/auth/logout", headers=csrf)).status_code == 204
        assert (await asyncio.wait_for(stream, timeout=2)).status_code == 200
        assert (await client.get("/api/events")).status_code == 401
        assert (await client.get("/auth/session")).json()["authenticated"] is False
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)


async def test_two_keys_wav_no_restart_no_secrets_to_engine(owner, monkeypatch):
    client, app = owner
    supervisor = app.state.supervisor
    settings = app.state.settings
    secrets_env = ("ADMIN_USERNAME", "ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE", "API_KEY")
    for key in secrets_env:
        monkeypatch.setenv(key, key + "-private-value")
    spawned = []
    real_spawn = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        spawned.append(kwargs["env"])
        return await real_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await supervisor.deploy(EngineConfig())
    pid = supervisor.process.pid
    assert supervisor.status == "ready" and pid
    assert len(spawned) >= 2 and all(not set(secrets_env).intersection(env) for env in spawned)
    login_result = await login(client)
    csrf = {"Origin": ORIGIN, "X-CSRF-Token": login_result.json()["csrf_token"]}
    assert (await client.put("/api/settings/compare", headers=csrf, json={"enabled": True})).json() == {"enabled": True}
    assert (await client.get("/api/status")).json()["compare_enabled"] is True
    assert (await client.post("/api/compare", headers=csrf)).json()["error"]["code"] == "missing_file"
    a, b = [(await client.post("/api/keys", json={"name": n}, headers=csrf)).json() for n in ["A", "B"]]
    for record in (a, b):
        assert record["key"].encode() not in app.state.owner_store.path.read_bytes()
    sent = []
    real_send = httpx.AsyncClient.send
    async def spy(self, request, *args, **kwargs):
        if request.url.port == settings.engine_port and request.url.path.endswith("transcriptions"):
            sent.append(request)
        return await real_send(self, request, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "send", spy)
    async with http(app) as machine:
        async def transcribe(record):
            return await machine.post("/v1/audio/transcriptions", files={"file": ("sample.wav", wav_bytes(), "audio/wav")},
                                      headers={"Authorization": "Bearer " + record["key"], "Cookie": "synthetic=value"})
        assert (await transcribe(a)).status_code == 200
        assert (await transcribe(b)).status_code == 200
        assert (await client.post(f"/api/keys/{a['id']}/revoke", headers=csrf)).status_code == 204
        assert (await transcribe(a)).status_code == 401
        assert (await transcribe(b)).status_code == 200
    assert supervisor.process.pid == pid and len(sent) == 3
    assert all(not req.url.query and not {"cookie", "authorization", "x-csrf-token"}.intersection(req.headers) for req in sent)


async def test_compare_setting_requires_owner_origin_csrf_and_non_url_auth(owner):
    client, app = owner
    path = "/api/settings/compare"
    payload = {"enabled": True}
    assert (await client.put(path, json=payload)).status_code == 401
    result = await login(client)
    csrf = {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}
    key = (await client.post("/api/keys", json={"name": "client"}, headers=csrf)).json()["key"]
    async with http(app) as machine:
        bearer = {"Authorization": f"Bearer {key}", **csrf}
        assert (await machine.put(path, headers=bearer, json=payload)).status_code == 401
    for headers, code in (
        ({"Origin": ORIGIN}, "invalid_csrf"),
        ({**csrf, "X-CSRF-Token": "wrong"}, "invalid_csrf"),
        ({**csrf, "Origin": "https://another.test"}, "invalid_origin"),
    ):
        refused = await client.put(path, headers=headers, json=payload)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == code
    assert (await client.put(path, headers=csrf, params={"api_key": key}, json=payload)).status_code == 400
    assert (await client.get("/api/status")).json()["compare_enabled"] is False
    assert (await client.put(path, headers=csrf, json=payload)).json() == payload
    assert (await client.get("/api/status")).json()["compare_enabled"] is True


async def test_storage_lost_while_serving(owner):
    client, app = owner
    assert (await login(client)).status_code == 200
    path = app.state.owner_store.path
    path.unlink()
    for route in ("/api/status", "/v1/models"):
        response = await client.get(route, headers={"Authorization": "Bearer synthetic"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "owner_store_unavailable"
    assert not path.exists()


async def test_owner_db_requires_explicit_keyed_rollback(owner, managed):
    client, app = owner
    good = await login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": good.json()["csrf_token"]}
    known = (await client.post("/api/keys", json={"name": "old"}, headers=headers)).json()
    assert (await client.post(f"/api/keys/{known['id']}/revoke", headers=headers)).status_code == 204
    local = managed.model_copy(update={"owner_access": False})
    for flag, key in [(False, ""), (False, "fresh-local"), (True, ""), (True, known["key"])]:
        with pytest.raises(ValueError):
            create_app(local.model_copy(update={"allow_owner_local_rollback": flag, "api_key": key}))
    rolled_back = create_app(local.model_copy(update={"allow_owner_local_rollback": True, "api_key": "fresh-local"}))
    async with http(rolled_back) as local_client:
        assert (await local_client.get("/api/status")).status_code == 401
        assert (await local_client.get("/api/status", headers={"Authorization": "Bearer fresh-local"})).status_code == 200


async def test_fingerprint_login_and_console(owner):
    client, _ = owner
    page = await client.get("/login")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert '/static/login.js?v=' in page.text
    await login(client)
    page = await client.get("/")
    assert '/static/owner.js?v=' in page.text


async def test_local_auth_contract(client_stopped):
    response = await client_stopped.get("/auth/session")
    assert response.json() == {"mode": "local"} and response.headers["cache-control"] == "no-store"
    assert (await client_stopped.get("/api/keys")).status_code == 404
