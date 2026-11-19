"""Регрессии браузерного контракта: uv run --with playwright pytest tests/test_owner_ui.py (Chrome)."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest


STATIC = Path(__file__).resolve().parents[1] / "console" / "static"
NOW = "2026-11-19T10:05:03+00:00"
SECRET = "one-time-browser-test-value"


@pytest.fixture
async def owner_ui(live_client):
    pw = pytest.importorskip("playwright.async_api")
    origin = str(live_client.base_url)
    config = {"authenticated": True, "rows": [], "requests": [], "errors": [], "fail": {}}

    async with pw.async_playwright() as driver:
        browser = await driver.chromium.launch(channel="chrome")
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            page.on("pageerror", lambda exc: config["errors"].append(str(exc)))
            await page.add_init_script("""if (!sessionStorage.seeded) {
                localStorage.setItem('stt_console_key','discard-before-any-request'); sessionStorage.seeded='1';
            }""")

            async def serve(route):
                req = route.request
                path = urlsplit(req.url).path
                headers = await req.all_headers()
                config["requests"].append((req.url, req.method, headers))
                if path in config["fail"]:
                    return await route.fulfill(status=config["fail"].pop(path), json={})
                if path == "/login":
                    return await route.fulfill(path=STATIC / "login.html", content_type="text/html")
                if path == "/auth/session":
                    session = {"mode": "owner", "authenticated": config["authenticated"]}
                    if config["authenticated"]:
                        session["csrf_token"] = "browser-test-csrf"
                    return await route.fulfill(json=session)
                if path == "/auth/login":
                    if headers.get("x-kamerton-login") != "1" or req.method != "POST":
                        return await route.fulfill(status=403)
                    if req.post_data_json != {"username": "owner", "password": "browser-test-password"}:
                        return await route.fulfill(status=401)
                    config["authenticated"] = True
                    return await route.fulfill(json={"mode": "owner", "authenticated": True, "csrf_token": "browser-test-csrf"})
                if (path.startswith("/api/") or path == "/auth/logout") and not config["authenticated"]:
                    return await route.fulfill(status=401)
                if req.method != "GET" and headers.get("x-csrf-token") != "browser-test-csrf":
                    return await route.fulfill(status=403)
                if path == "/auth/logout":
                    config["authenticated"] = False
                    return await route.fulfill(status=204)
                if path == "/api/keys" and req.method == "GET":
                    return await route.fulfill(json={"keys": config["rows"]})
                if path == "/api/keys" and req.method == "POST":
                    row = {"id": str(len(config["rows"]) + 1), "name": req.post_data_json["name"], "created_at": NOW, "revoked_at": None}
                    config["rows"].append(row)
                    return await route.fulfill(status=201, json={**row, "key": SECRET})
                if path.startswith("/api/keys/") and path.endswith("/revoke"):
                    next(row for row in config["rows"] if row["id"] == path.split("/")[3])["revoked_at"] = NOW
                    return await route.fulfill(status=204)
                if path == "/api/client-models":
                    return await route.fulfill(json={"object": "list", "data": []})
                return await route.continue_()

            await page.route("**/*", serve)
            yield page, pw.expect, config, origin
            assert not config["errors"]
            for url, _, headers in config["requests"]:
                assert "authorization" not in headers
                assert not any(value in url for value in ("api_key=", SECRET, "discard-before-any-request", "browser-test-password"))
        finally:
            await browser.close()


async def test_login_expiry_and_logout_in_chrome(owner_ui):
    page, expect, config, origin = owner_ui
    config["authenticated"] = False
    await page.goto(origin + "/")
    await expect(page).to_have_url(origin + "/login")
    user, password = page.locator("#login-username"), page.locator("#login-password")
    await expect(user).to_have_attribute("autocomplete", "username")
    await expect(password).to_have_attribute("autocomplete", "current-password")
    await user.fill("owner")
    await password.fill("wrong")
    await password.press("Enter")
    await expect(page.get_by_role("alert")).to_contain_text("Incorrect username or password")
    await expect(password).to_be_focused()
    await page.locator("[data-lang=ru]").click()
    await expect(page.get_by_role("alert")).to_contain_text("Неверный логин или пароль")
    await password.fill("browser-test-password")
    await password.press("Enter")
    await expect(page.locator("#owner-logout")).to_have_text("Выйти")
    assert await page.evaluate("localStorage.getItem('stt_console_key')") is None
    config["authenticated"] = False
    # Ближайший фоновый запрос получает 401, даже если владелец ничего не нажимает.
    await expect(page).to_have_url(origin + "/login", timeout=12000)
    await expect(password).to_be_empty()
    await user.fill("owner")
    await password.fill("browser-test-password")
    await password.press("Enter")
    config["fail"]["/auth/logout"] = 503
    await page.locator("#owner-logout").click()
    await expect(page.locator("#logout-error")).to_contain_text("Выход не подтверждён")
    await page.locator("#owner-logout").click()
    await expect(page).to_have_url(origin + "/login")
    assert not config["authenticated"]


async def test_owner_keys_keyboard_and_copy_in_chrome(owner_ui):
    page, expect, config, origin = owner_ui
    await page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    config["fail"]["/api/keys"] = 503
    await page.goto(origin + "/#api")
    await page.locator("#key-retry").click()
    await expect(page.locator("#key-list-status")).to_have_text("No keys yet.")
    await expect(page.locator("#edit-key")).to_be_disabled()
    await expect(page.locator("#link-docs")).to_have_attribute("href", "/api/docs")
    await page.locator("#service-models summary").click()
    await expect(page.locator("#model-response")).to_contain_text('"data"')
    assert any(url.endswith("/api/events") for url, _, _ in config["requests"])
    assert not any("/v1/models" in url for url, _, _ in config["requests"])
    name = page.locator("#key-name")
    await name.fill("Laptop <client>")
    config["fail"]["/api/keys"] = 503
    await name.press("Enter")
    await expect(page.locator("#key-notice")).to_contain_text("not confirmed")
    await expect(page.locator("#secret-dialog")).to_be_hidden()
    await name.press("Enter")
    secret = page.locator("#key-secret")
    await expect(secret).to_have_text(SECRET)
    assert SECRET not in await page.evaluate("JSON.stringify(localStorage) + JSON.stringify(sessionStorage)")
    await page.locator("#key-copy").press("Enter")
    await expect(page.locator("#key-copy-status")).not_to_be_empty()
    assert await page.evaluate("navigator.clipboard.readText()") == SECRET
    await page.keyboard.press("Escape")
    await expect(secret).to_be_empty()
    await expect(name).to_be_focused()
    revoke = page.get_by_role("button", name="Revoke key for Laptop <client>")
    await revoke.click()
    await expect(page.locator("#revoke-cancel")).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(revoke).to_be_focused()
    assert config["rows"][0]["revoked_at"] is None
    await revoke.press("Enter")
    config["fail"]["/api/keys/1/revoke"] = 503
    await page.locator("#revoke-confirm").click()
    await expect(page.locator("#revoke-error")).to_contain_text("Try again")
    await page.locator("#revoke-confirm").click()
    await expect(page.locator("#key-list")).to_contain_text("Revoked:")
    await expect(revoke).to_have_count(0)


async def test_key_cleanup_and_responsive_locales_in_chrome(owner_ui):
    page, expect, _, origin = owner_ui
    await page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
    await page.goto(origin + "/#api")
    for width in (320, 390, 768, 1440):
        await page.set_viewport_size({"width": width, "height": 844})
        for lang in ("ru", "en"):
            await page.locator(f".lang-option[data-lang={lang}]:visible").click()
            await expect(page.locator("html")).to_have_attribute("lang", lang)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.set_viewport_size({"width": 320, "height": 640})
    await page.locator(".mobile [data-lang=ru]").click()
    await page.locator("#key-name").fill("Ноутбук")
    await page.locator("#key-create").click()
    await expect(page.locator("#secret-dialog")).to_be_visible()
    await page.locator("#key-copy").click()
    await expect(page.locator("dialog .manual textarea")).to_have_value(SECRET)
    await expect(page.locator("dialog .manual textarea")).to_be_focused()
    await page.evaluate("location.hash = 'status'")
    await expect(page.locator("#key-secret")).to_be_empty()
    await expect(page.locator(".manual")).to_have_count(0)
    await page.locator("#mobile-nav").select_option("api")
    await expect(page.locator("#key-list")).to_contain_text("Ноутбук")
    assert SECRET not in await page.content()


async def test_owner_example_requires_https_but_local_accepts_http(owner_ui):
    page, expect, _, origin = owner_ui
    await page.goto(origin + "/#api")
    address, copy = page.locator("#example-origin"), page.locator('[data-target="snip-curl"]')
    await address.fill("http://dictation.example")
    await expect(address).to_have_attribute("aria-invalid", "true")
    await expect(page.locator("#address-error")).to_contain_text("https://")
    await expect(copy).to_be_disabled()
    await expect(page.locator("#snip-curl")).to_have_text("—")
    await address.fill("https://dictation.example")
    await expect(page.locator("#base-url")).to_have_text("https://dictation.example/v1")
    await expect(copy).to_be_enabled()
    # Без заглушек тот же uvicorn работает в режиме local.
    await page.unroute_all(behavior="wait")
    await page.reload()
    await address.fill("http://dictation.example")
    await expect(address).to_have_attribute("aria-invalid", "false")
    await expect(page.locator("#base-url")).to_have_text("http://dictation.example/v1")
    await expect(copy).to_be_enabled()


async def test_session_error_never_falls_back_to_stored_key(owner_ui):
    page, expect, config, origin = owner_ui
    config["fail"]["/auth/session"] = 503
    await page.goto(origin + "/#api")
    await expect(page.locator("#session-retry")).to_be_visible()
    assert not any(urlsplit(url).path.startswith("/api/") for url, _, _ in config["requests"])
    await page.locator("#session-retry").click()
    await expect(page.locator("#key-list-status")).to_have_text("No keys yet.")
    assert await page.evaluate("localStorage.getItem('stt_console_key')") is None
