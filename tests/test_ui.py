import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import route_paths, wav_bytes


STATIC = Path(__file__).resolve().parents[1] / "console" / "static"


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, _tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            assert attrs["id"] not in self.by_id, attrs["id"]
            self.by_id[attrs["id"]] = attrs


def test_glossary_starts_locked_and_file_pickers_are_native():
    markup = Inputs()
    markup.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    for name in ("glossary-editor", "btn-glossary", "config-editor"):
        assert "disabled" in markup.by_id[name]
    assert markup.by_id["opt-boost"]["step"] == "any"  # API допускает, например, 4.25
    assert "hidden" in markup.by_id["btn-compare"]  # до файла только действие выбора
    for name in ("file-input", "compare-file", "glossary-file"):
        attrs = markup.by_id[name]
        assert attrs["type"] == "file" and "data-i18n-aria" in attrs
        assert "hidden" not in attrs and attrs.get("tabindex") != "-1"


def test_example_origin_is_isolated_from_transport_and_key():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'return url.origin + "/v1"' in js or 'url.origin + "/v1" : null' in js
    snippets = js.split("function renderSnippets()", 1)[1].split("\n}", 1)[0]
    assert "clientBase()" in snippets and '"/api/docs?api_key="' in snippets
    assert not re.search(r"\b(?:fetch|api)\(", snippets)
    assert "state.key" not in snippets.split('$("link-docs")')[0]
    assert 'mode: "same-origin"' in js and 'redirect: "error"' in js
    assert 'api("/v1/models")' in js and '/v1/models?api_key=' not in js


def test_journal_and_comparison_use_separate_server_contracts():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'api("/api/journal")' in js and 'api("/api/compare"' in js
    for field in ("variant_a", "variant_b", "file"):
        assert f'form.append("{field}"' in js
    assert 'compare_enabled === true' in js
    assert 'innerHTML' not in js



async def test_index_served_with_expected_sections(client_ready):
    """Разметка по-английски: это язык по умолчанию, русский включается кнопкой."""
    html = (await client_ready.get("/")).text
    for page in ("dict", "compare", "api", "models", "settings", "glossary", "status", "journal"):
        assert f'data-page="{page}"' in html
        assert f'href="#{page}"' in html
    assert "Kamerton" in html and "Dictation" in html


async def test_index_links_its_own_assets(client_ready):
    html = (await client_ready.get("/")).text
    assert "/static/style.css" in html and "/static/app.js" in html
    assert "/static/i18n.js" in html
    assert (await client_ready.get("/static/style.css")).status_code == 200
    assert (await client_ready.get("/static/app.js")).status_code == 200
    assert (await client_ready.get("/static/i18n.js")).status_code == 200


async def test_app_js_only_calls_existing_endpoints(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    called = set(re.findall(r"""fetch\(\s*["'`](/[^"'`?]+)""", js))
    called |= set(re.findall(r"""api\(\s*["'](/[^"'?]+)""", js))
    routes = route_paths(client_ready.app)
    assert called, "app.js should talk to the console API"
    assert called <= routes, f"unknown endpoints: {called - routes}"


async def test_event_source_url_is_a_real_route(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    assert 'EventSource("/api/events"' in js
    assert "/api/events" in route_paths(client_ready.app)


async def test_mic_recording_produces_wav_client_side(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    assert "AudioContext" in js  # capture without MediaRecorder containers
    assert "RIFF" in js  # WAV assembled in the browser, so no ffmpeg in the image
    assert "getUserMedia" in js


async def test_ui_shows_latency_comparison(client_ready):
    html = (await client_ready.get("/")).text
    assert "bar-audio" in html and "bar-work" in html
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "быстрее реального времени" in texts and "faster than real time" in texts


async def test_ui_shows_how_much_of_the_glossary_reaches_the_engine(client_ready):
    html = (await client_ready.get("/")).text
    assert "glossary-reach" in html and "dropped-hint" in html
    js = (await client_ready.get("/static/app.js")).text
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "зачёркнутое движок выбросит" in texts and "все фразы дойдут до движка" in texts
    assert "usable_count" in js and "dropped" in js
    # Подсказка собирается из состава словаря, а не лежит готовой в разметке:
    # «пишите по-русски» неверно для e2e_rnnt, где латиница есть.
    assert "по-русски" in texts and "по-русски" not in html
    assert "alphabet" in js


async def test_edits_are_staged_until_applied(client_ready):
    """Каждое применение перезагружает движок, поэтому правки копятся до кнопки."""
    js = (await client_ready.get("/static/app.js")).text
    assert 'glossaryText() === glossary.applied' in js
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "нажмите «Применить»" in texts and 'press \u201cApply\u201d' in texts


async def test_ui_advises_transliteration_and_never_lowercasing(client_ready):
    """The engine retries a phrase lowercased itself, so only the alphabet is left."""
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "опенвиспр вместо OpenWhispr" in texts
    assert "Пишите строчными" not in texts


async def test_glossary_is_a_field_of_chips_with_import_and_export(client_ready):
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    for marker in ["btn-export", "glossary-file", "btn-add", "add-phrase", "tag-list"]:
        assert marker in html
    assert "<textarea" not in html  # список правится чипами, а не текстовым полем
    assert "importGlossary" in js and "exportGlossary" in js and "addPhrases" in js
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "глоссарий.txt" in texts and "glossary.txt" in texts


async def test_a_phrase_already_in_the_list_is_not_added_twice(client_ready):
    """Сравнение как у движка: регистр и «ё» не делают фразу новой."""
    js = (await client_ready.get("/static/app.js")).text
    assert 'toLowerCase().replace(/ё/g, "е")' in js
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "уже в списке" in texts and "flashChip" in js


async def test_dropped_phrases_are_struck_through_in_place(client_ready):
    css = (await client_ready.get("/static/style.css")).text
    js = (await client_ready.get("/static/app.js")).text
    assert '.tag[data-dropped="true"] .tag-text { text-decoration: line-through' in css
    assert "dataset.dropped" in js
    # Отдельного свёрнутого списка отброшенных больше нет — чипы и есть список.
    html = (await client_ready.get("/")).text
    assert "dropped-list" not in html and "dropped-summary" not in html


async def test_status_shows_totals_and_recent_files(client_ready):
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert "ro-total" in html and "recent-rows" in html
    assert "files, all time" in html
    assert "файлов за всё время" in (await client_ready.get("/static/i18n.js")).text
    assert "renderRecent" in js and "avg_elapsed_total" in js


async def test_microphone_button_explains_itself_without_https(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "window.isSecureContext" in js
    assert "только на localhost или по HTTPS" in texts
    assert "only on localhost or over HTTPS" in texts


async def test_page_is_revalidated_and_assets_are_fingerprinted(client_ready):
    """Иначе после выкладки браузер держит старую разметку со свежим скриптом."""
    response = await client_ready.get("/")
    assert "no-cache" in response.headers.get("cache-control", "")
    match = re.search(r'/static/app\.js\?v=([0-9a-f]{8})', response.text)
    assert match, "ссылка на скрипт без отпечатка содержимого"
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{8}', response.text)
    assert (await client_ready.get(f"/static/app.js?v={match.group(1)}")).status_code == 200


async def test_deploy_sends_every_engine_field(client_ready):
    """Иначе поля, которых нет в форме, молча берутся из старого состояния -
    так HOTWORDS_DEFAULT=1 в .env не доходил до движка."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert "opt-hotwords-default" in html and "opt-boost" in html
    for field in ("hotwords_default", "hotwords_boost"):
        assert field in js, f"deploy() не посылает {field}"


async def test_ui_shows_env_divergence_and_offers_to_deploy_it(client_ready):
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert "env-diff" in html and "btn-deploy-env" in html
    assert "env.diverges" in js and "env_missing" in js
    assert "btn-glossary-env" in html


async def test_head_cards_show_badges_with_their_source(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    css = (await client_ready.get("/static/style.css")).text
    assert "head.badge" in js and "badge_note" in js
    assert ".head-badge" in css


async def test_deploy_progress_next_to_explicit_apply(client_ready):
    """Цель и процент берутся из статуса, индикатор стоит у явной кнопки запуска."""
    html = (await client_ready.get("/")).text
    form = re.search(r'<form id="config-form".*?</form>', html, re.S).group()
    assert 'id="btn-deploy" type="submit"' in form
    assert '<progress id="deploy-progress"' in form
    js = (await client_ready.get("/static/app.js")).text
    assert "status.deploying" in js and "status.download_percent" in js
    change = re.search(r'\$\("heads"\)\.onchange\s*=.*?};', js, re.S).group()
    assert "state.selected" in change and "deploy(" not in change


async def test_head_cards_are_built_once_and_updated_in_place(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    assert "state.heads" in js
    # пересборка всех карточек на каждое событие и была причиной моргания
    assert js.count('container.textContent = ""') == 0


async def test_busy_state_comes_from_the_server_not_the_post(client_ready):
    """POST /api/deploy возвращает 202 сразу, а развертывание идет еще минуты."""
    js = (await client_ready.get("/static/app.js")).text
    assert "state.busy = true" not in js and "state.busy = false" not in js


async def test_rollback_verdict_stays_on_the_card_that_was_clicked(client_ready):
    """Откат кончается зелёным `ready`, а нажимали другую голову: без этой ветки о
    неудаче говорила бы только пилюля наверху — ровно то, что мы и убирали."""
    js = (await client_ready.get("/static/app.js")).text
    assert "clickedTarget" in js
    assert 'status.status === "ready" && clickedTarget' in js
    # Откат узнается по коду статуса, а не по русскому слову в тексте: текст двуязычный.
    assert "isRollback(status)" in js and 'indexOf("rollback.") === 0' in js


async def test_connection_section_links_api_docs_and_models(client_ready):
    """Схема управляющего API и список моделей - две ссылки, за которыми чаще всего
    идут в чужую документацию вместо своей консоли."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert 'id="link-docs"' in html and 'id="snip-models"' in html
    assert 'id="connect-docs"' in html
    assert "/api/docs" in js and "/v1/models" in js


async def test_ui_offers_window_concurrency_and_names_its_condition(client_ready):
    """Селект бесполезен без пула 2: движок берет свободный слот без ожидания, и при
    пуле 1 параллельность молча ничего не делает. Условие должно стоять в интерфейсе."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert "opt-window-concurrency" in html
    texts = (await client_ready.get("/static/i18n.js")).text
    assert "части длинной записи обрабатываются по очереди" in texts and "Unavailable with one simultaneous request" in texts
    assert "file_window_concurrency" in js


# --- два языка интерфейса ------------------------------------------------------


async def test_english_is_the_default_and_russian_is_a_switch(client_ready):
    """Английский - язык по умолчанию всегда: настройка браузера не спрашивается,
    иначе русский посетитель никогда бы не увидел язык, на котором написан проект."""
    html = (await client_ready.get("/")).text
    texts = (await client_ready.get("/static/i18n.js")).text
    assert '<html lang="en">' in html
    assert 'data-lang="en"' in html and 'data-lang="ru"' in html
    assert 'const DEFAULT_LANG = "en"' in texts
    assert "navigator.language" not in texts
    assert "localStorage" in texts  # выбор языка переживает перезагрузку


async def test_both_languages_cover_the_same_keys(client_ready):
    """Пропущенный ключ показал бы английскую фразу в русском интерфейсе - молча."""
    texts = (await client_ready.get("/static/i18n.js")).text
    blocks = re.findall(r"\n  (en|ru): \{(.*?)\n  \},", texts, re.S)
    assert {name for name, _body in blocks} == {"en", "ru"}
    keys = {name: set(re.findall(r'"([\w.]+)":', body)) for name, body in blocks}
    assert keys["en"] == keys["ru"], keys["en"] ^ keys["ru"]
    assert len(keys["en"]) > 100


async def test_interface_strings_live_in_the_dictionary_not_in_the_code(client_ready):
    """Половина фраз в словаре, половина в коде - и переведена будет половина."""
    js = (await client_ready.get("/static/app.js")).text
    # Кириллица в app.js допустима только в комментариях и в сравнении «ё» с «е».
    code_lines = [
        line for line in js.splitlines()
        if re.search(r'["\'][^"\']*[А-Яа-я]', line)
        and not line.lstrip().startswith(("//", "/*", "*"))
        and "ё" not in line
    ]
    assert not code_lines, code_lines


async def test_head_descriptions_come_translated_from_the_server(client_ready):
    """Подписи голов живут в каталоге, поэтому язык спрашивается у сервера."""
    js = (await client_ready.get("/static/app.js")).text
    assert "/api/models?lang=" in js

    english = (await client_ready.get("/api/models", params={"lang": "en"})).json()
    russian = (await client_ready.get("/api/models", params={"lang": "ru"})).json()
    unknown = (await client_ready.get("/api/models", params={"lang": "xx"})).json()
    by_id = {head["id"]: head for head in english["heads"]}
    assert "vocabulary" in by_id["e2e_rnnt"]["subtitle"]
    assert by_id["e2e_rnnt"]["badge"] == "low error on FLEURS ru"
    ru_by_id = {head["id"]: head for head in russian["heads"]}
    assert ru_by_id["e2e_rnnt"]["badge"] == "низкая ошибка на FLEURS ru"
    # Названия моделей не переводятся, переводятся только пояснения.
    assert ru_by_id["e2e_rnnt"]["title"] == by_id["e2e_rnnt"]["title"]
    assert unknown["heads"] == english["heads"]  # неизвестный язык - английский


async def test_glossary_warnings_are_rendered_and_translated(client_ready):
    """Коды с сервера превращаются в фразы здесь, значит нужны в обоих словарях."""
    js = (await client_ready.get("/static/app.js")).text
    assert 't("issue." + code' in js
    assert 'id="glossary-issues"' in (await client_ready.get("/")).text

    texts = (await client_ready.get("/static/i18n.js")).text
    blocks = re.findall(r"\n  (en|ru): \{(.*?)\n  \},", texts, re.S)
    keys = {name: set(re.findall(r'"([\w.]+)":', body)) for name, body in blocks}
    for code in ("weight_off", "weight_ignored", "weight_invalid", "comment", "tab", "short"):
        assert f"issue.{code}" in keys["en"]
        assert f"issue.{code}" in keys["ru"]


async def test_glossary_intro_states_the_selection_rule_not_the_weight_syntax(client_ready):
    """Главное правило всех источников - добавлять только то, что движок не берёт.

    Про вес интерфейс больше ничего не обещает: движок читает его как
    выключатель, и старая подпись «вес подсказки» обещала силу, которой нет.
    """
    texts = (await client_ready.get("/static/i18n.js")).text
    en = re.search(r'"glossary\.intro":\s*\n?\s*"([^"]*)"', texts).group(1)
    assert "gets wrong without help" in en
    assert "vertical bar" not in en


async def test_mobile_feedback_and_model_list_in_chrome(live_client):
    """Браузерная регрессия: uv run --with playwright pytest -k mobile_feedback (нужен Chrome)."""
    pw = pytest.importorskip("playwright.async_api")
    from console.state import EngineConfig

    app = live_client.app
    app.state.settings.api_key = "ui-test-only"
    app.state.settings.max_upload_mb = 1
    await app.state.supervisor.deploy(EngineConfig())
    async with pw.async_playwright() as driver:
        browser = await driver.chromium.launch(channel="chrome")
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.add_init_script("localStorage.setItem('stt_console_key', 'ui-test-only')")
            await page.goto(str(live_client.base_url))
            nav = page.locator("#mobile-nav")
            await page.locator("#file-mode").click()
            await page.locator("#file-input").set_input_files({
                "name": "sample.wav", "mimeType": "audio/wav", "buffer": b"0" * (1024**2 + 1),
            })
            await page.locator("#btn-send").click()
            await pw.expect(page.locator("#result-text")).to_contain_text("upload limit")
            await page.locator(".mobile [data-lang=ru]").click()
            await pw.expect(page.locator("#result-text")).to_contain_text("лимит загрузки")

            await page.set_viewport_size({"width": 320, "height": 844})
            await page.locator("#file-input").set_input_files({
                "name": "sample.wav", "mimeType": "audio/wav", "buffer": wav_bytes(),
            })
            await page.locator("#btn-send").click()
            await pw.expect(page.locator("#result")).to_be_visible()
            assert await page.locator(".latency > div").evaluate_all("""rows => rows.every(row => {
                const range = document.createRange(); range.selectNodeContents(row.firstElementChild);
                return range.getBoundingClientRect().right < row.children[1].getBoundingClientRect().left;
            })""")

            await nav.select_option("glossary")
            assert await page.locator("#btn-add").evaluate("""button => {
                const range = document.createRange(); range.selectNodeContents(button);
                return range.getClientRects().length === 1;
            }""")
            await page.locator("#btn-export").click()
            await page.locator(".mobile [data-lang=en]").click()
            await pw.expect(page.locator("#glossary-hint")).to_have_text(
                await page.evaluate("i18n.t('glossary.empty')")
            )
            await nav.select_option("api")
            async with page.expect_request(lambda r: r.url.endswith("/v1/models")) as req:
                await page.locator("#service-models summary").click()
            assert await (await req.value).header_value("authorization") == "Bearer ui-test-only"
            await pw.expect(page.locator("#model-response")).to_contain_text('"data"')
        finally:
            await browser.close()


@pytest.fixture
async def chrome_page(live_client):
    pw = pytest.importorskip("playwright.async_api")
    async with pw.async_playwright() as driver:
        browser = await driver.chromium.launch(channel="chrome", args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        ])
        try:
            yield await browser.new_page(), pw.expect
        finally:
            await browser.close()


async def test_stop_after_failed_deploy_and_retry_in_chrome(chrome_page, live_client, monkeypatch):
    from console.state import EngineConfig

    page, expect = chrome_page
    supervisor = live_client.app.state.supervisor
    await supervisor.deploy(EngineConfig())
    await page.goto(str(live_client.base_url) + "/#models")
    monkeypatch.setenv("FAKE_DOWNLOAD_SLOW", "0.6")
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "disk")
    await page.locator("#target-ml_ctc").check()
    await page.locator('[data-page=models] a[href="#settings"]').click()
    await page.locator("#btn-deploy").click()
    await page.locator('#railnav a[href="#status"]').click()
    await expect(page.locator("#state-pill")).to_have_attribute("data-state", "downloading")
    stop = page.locator("#btn-stop")
    await expect(stop).to_be_hidden()
    await expect(page.locator("#state-pill")).to_have_attribute("data-state", "error")
    await expect(stop).to_be_visible()
    await expect(stop).to_be_enabled()
    await page.route("**/api/stop", lambda r: r.fulfill(status=503, body="Unavailable"), times=1)
    feedback = page.locator("#stop-feedback")
    await stop.click()
    await expect(feedback).to_have_text("Could not stop the engine. Error 503")
    async with page.expect_response(lambda r: r.url.endswith("/api/status")):
        pass  # Ждём следующего штатного опроса.
    await expect(feedback).to_contain_text("503")
    await page.locator(".rail [data-lang=ru]").click()
    await expect(feedback).to_have_text("Не удалось остановить движок. Ошибка 503")
    await stop.click()
    await expect(page.locator("#state-pill")).to_have_attribute("data-state", "stopped")
    await expect(stop).to_be_hidden()
    await expect(feedback).to_be_empty()
    assert not supervisor.process.is_running


async def test_bearer_switch_discards_manual_copy_in_chrome(chrome_page, live_client):
    page, expect = chrome_page
    await page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
    await page.goto(str(live_client.base_url) + "/#api")
    await expect(page.locator("#state-pill")).to_have_attribute("data-state", "stopped")
    bearer, copy = page.locator("#example-key"), page.locator('[data-target="snip-curl"]')
    await bearer.check()
    await copy.click()
    manual = page.locator(".manual textarea")
    assert "Authorization: Bearer" in await manual.input_value()
    await bearer.uncheck()
    await expect(manual).to_have_count(0)
    await copy.click()
    assert await manual.input_value() == await page.locator("#snip-curl").text_content()
    assert "Authorization" not in await manual.input_value()


async def test_deployment_dependencies_and_old_defaults_in_chrome(chrome_page, live_client):
    from console.state import EngineConfig

    page, expect = chrome_page
    supervisor = live_client.app.state.supervisor
    await supervisor.deploy(EngineConfig(variant="rnnt", punctuation="off", itn="on", pool_size=4,
                                         file_window_concurrency=3, hotwords_default=True, hotwords_boost=4.25))
    await page.goto(str(live_client.base_url) + "/#settings")
    pool, windows = page.locator("#opt-pool"), page.locator("#opt-window-concurrency")
    await expect(windows).to_have_value("3")
    await expect(page.locator("#opt-boost")).to_have_value("4.25")
    await expect(page.locator("#opt-punctuation")).to_have_value("off")
    await expect(page.locator("#opt-itn")).to_have_value("on")
    await pool.select_option("2")
    await expect(windows).to_have_value("2")
    await expect(windows.locator('option[value="3"]')).to_be_disabled()
    await pool.select_option("1")
    await expect(windows).to_be_disabled()
    await expect(windows).to_have_value("1")
    await page.locator("#opt-hotwords-default").select_option("false")
    await expect(page.locator("#opt-boost")).to_be_disabled()
    for lang, note in [("ru", "Недоступно"), ("en", "Unavailable")]:
        await page.locator(f'.lang-option[data-lang="{lang}"]:visible').click()
        await expect(page.locator("#window-note")).to_contain_text(note)
        await expect(page.locator("#boost-note")).to_contain_text(note)
    for variant in ("e2e_rnnt", "ml_ctc", "ml_ctc_large", "rnnt"):
        await page.locator('.cfghead a[href="#models"]').click()
        await page.locator("#target-" + variant).check()
        await page.locator('[data-page=models] a[href="#settings"]').click()
        await expect(page.locator("#opt-punctuation")).to_be_enabled()
        await expect(page.locator("#opt-itn")).to_be_enabled()
        await expect(page.locator("#punctuation-note")).to_contain_text({
            "rnnt": "Auto runs", "e2e_rnnt": "already adds", "ml_ctc": "CTC", "ml_ctc_large": "CTC",
        }[variant])
    async with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/deploy")) as posted:
        await page.locator("#btn-deploy").click()
    config = (await posted.value).post_data_json
    assert config["file_window_concurrency"] == config["pool_size"] == 1
    assert config["hotwords_default"] is False and "hotwords_boost" not in config


async def test_env_deploy_uses_exact_snapshot_and_rejects_conflict(chrome_page, live_client):
    from conftest import wait_for
    from console.state import EngineConfig

    page, expect = chrome_page
    supervisor = live_client.app.state.supervisor
    settings = live_client.app.state.settings
    await supervisor.deploy(EngineConfig(hotwords_default=False, hotwords_boost=5))
    settings.hotwords_default, settings.hotwords_boost = False, 8
    settings.pool_size, settings.file_window_concurrency = 1, 2
    requests = []
    page.on("request", lambda req: requests.append(req.post_data_json) if req.url.endswith("/api/deploy") else None)
    await page.goto(str(live_client.base_url) + "/#settings")
    env = page.locator("#btn-deploy-env")
    await expect(env).to_be_visible()
    await expect(env).to_be_disabled()
    await expect(page.locator("#env-error")).to_contain_text("FILE_WINDOW_CONCURRENCY")
    await page.locator('.lang-option[data-lang="ru"]:visible').click()
    await expect(page.locator("#env-error")).to_contain_text("частей одновременно: 2")
    await env.evaluate("button => button.click()")
    assert not requests and supervisor.current.hotwords_boost == 5
    settings.file_window_concurrency = 1
    await page.reload()
    await expect(env).to_be_enabled()
    await expect(page.locator("#opt-boost")).to_be_disabled()
    await page.locator("#opt-punctuation").select_option("on")
    await page.route("**/api/deploy", lambda r: r.fulfill(status=503), times=1)
    await env.click()
    await expect(page.locator("#env-error")).to_contain_text("503")
    await expect(page.locator("#opt-punctuation")).to_have_value("on")
    await expect(page.locator("#draft-note")).not_to_be_empty()
    await env.click()
    await wait_for(lambda: supervisor.status == "ready" and supervisor.current and supervisor.current.hotwords_boost == 8)
    assert requests == [supervisor.env_config().to_dict()] * 2
    assert supervisor.current.hotwords_default is False and supervisor.current.punctuation == "auto"


async def test_microphone_and_selected_filename_in_chrome(chrome_page, live_client):
    from console.state import EngineConfig

    page, expect = chrome_page
    await live_client.app.state.supervisor.deploy(EngineConfig())
    await page.goto(str(live_client.base_url))
    await page.locator("#btn-record").click()
    # Реальный AudioWorklet получает кадры виртуального устройства Chrome, а не пользовательского микрофона.
    await page.wait_for_function("state.recording?.chunks.reduce((n, a) => n + a.length, 0) >= 8000")
    await page.locator("#btn-record").click()
    await expect(page.locator("#result")).to_be_visible()
    await page.locator("#file-mode").click()
    name = "synthetic sample 07.wav"
    await page.locator("#file-input").set_input_files({"name": name, "mimeType": "audio/wav", "buffer": wav_bytes()})
    await expect(page.locator("#source-state")).to_have_text(name)
    await page.locator("#btn-send").click()
    await expect(page.locator("#result")).to_be_visible()
    await page.locator('#railnav a[href="#status"]').click()
    rows = page.locator("#recent-rows tr")
    await expect(rows.nth(0)).to_contain_text(name)
    await expect(rows.nth(0)).to_contain_text("Uploaded file")
    await expect(rows.nth(1)).to_contain_text("microphone.wav")
    await expect(rows.nth(1)).to_contain_text("Live microphone")


async def test_compare_switch_without_engine_restart_in_chrome(chrome_page, live_client, monkeypatch, tmp_path):
    from conftest import wait_for
    from console.state import EngineConfig

    page, expect = chrome_page
    supervisor = live_client.app.state.supervisor
    await supervisor.deploy(EngineConfig(variant="rnnt"))
    await supervisor.deploy(EngineConfig(variant="e2e_rnnt"))
    pid = supervisor.process.pid
    origin = str(live_client.base_url)
    await page.goto(origin + "/#compare")
    toggle = page.locator("#compare-enabled")
    await expect(page.locator("#compare-toggle-note")).to_contain_text("without access protection")
    await expect(toggle).to_be_disabled()
    live_client.app.state.settings.api_key = "ui-synthetic-local"
    await page.evaluate("localStorage.setItem('stt_console_key','ui-synthetic-local')")
    await page.reload()
    await page.route("**/api/settings/compare", lambda r: r.fulfill(status=503, json={
        "error": {"code": "compare_settings_unavailable", "message": "Temporary write failure"},
    }), times=1)
    await toggle.check()
    await expect(page.locator("#compare-toggle-error")).to_contain_text("could not be saved")
    await expect(toggle).not_to_be_checked()
    assert not supervisor.compare_enabled
    async with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/settings/compare")) as sent:
        await toggle.check()
    assert (await sent.value).post_data_json == {"enabled": True}
    await expect(page.locator("#compare-form")).to_be_visible()
    await page.reload()
    await expect(toggle).to_be_checked()
    await page.locator("#compare-file").set_input_files({"name": "comparison.wav", "mimeType": "audio/wav", "buffer": wav_bytes()})
    await page.locator("#btn-compare").click()
    await expect(page.locator("#compare-results .panel")).to_have_count(2)
    for width in (320, 390, 1440):
        await page.set_viewport_size({"width": width, "height": 844 if width < 500 else 900})
        for lang in ("ru", "en"):
            await page.locator(f'.lang-option[data-lang="{lang}"]:visible').click()
            await expect(page.locator("#compare-form")).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if folder := os.environ.get("KAMERTON_UI_ARTIFACTS"):
                Path(folder).mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=Path(folder) / f"compare-{width}-{lang}.png")
    await toggle.uncheck()
    await expect(page.locator("#compare-form")).to_be_hidden()
    await expect(page.locator("#compare-results .panel")).to_have_count(0)
    assert not supervisor.compare_enabled and supervisor.process.pid == pid

    # Выключение во время уже запущенного CLI закрывает только новые задания.
    from console import compare

    record = tmp_path / "synthetic-compare-started.jsonl"
    cli_env = compare._cli_env() | {"FAKE_COMPARE_RECORD": str(record), "FAKE_COMPARE_DELAY": "0.5"}
    monkeypatch.setattr(compare, "_cli_env", lambda: cli_env.copy())
    await toggle.check()
    await page.locator("#btn-compare").click()
    await wait_for(record.exists)
    await toggle.uncheck()
    await expect(page.locator("#compare-form")).to_be_hidden()
    await expect(page.locator("#compare-results .panel")).to_have_count(2)
    assert not supervisor.compare_enabled and supervisor.process.pid == pid
