import re

from conftest import route_paths


async def test_index_served_with_expected_sections(client_ready):
    html = (await client_ready.get("/")).text
    for marker in ["Статус", "Модели", "Проверка", "Глоссарий", "Подключение", "Логи"]:
        assert marker in html


async def test_index_links_its_own_assets(client_ready):
    html = (await client_ready.get("/")).text
    assert "/static/style.css" in html and "/static/app.js" in html
    assert (await client_ready.get("/static/style.css")).status_code == 200
    assert (await client_ready.get("/static/app.js")).status_code == 200


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
    js = (await client_ready.get("/static/app.js")).text
    assert "быстрее реального времени" in js


async def test_ui_shows_how_much_of_the_glossary_reaches_the_engine(client_ready):
    html = (await client_ready.get("/")).text
    assert "glossary-reach" in html and "dropped-hint" in html
    js = (await client_ready.get("/static/app.js")).text
    assert "зачёркнутое движок выбросит" in js and "все фразы дойдут до движка" in js
    assert "usable_count" in js and "dropped" in js
    # Подсказка собирается в JS из состава словаря, а не лежит готовой в разметке:
    # «пишите строчными» неверно для e2e_rnnt, где заглавные есть.
    assert "строчными" in js and "строчными" not in html
    assert "alphabet" in js


async def test_edits_are_staged_until_applied(client_ready):
    """Каждое применение перезагружает движок, поэтому правки копятся до кнопки."""
    js = (await client_ready.get("/static/app.js")).text
    assert 'glossaryText() === glossary.applied' in js
    assert "нажмите «Применить»" in js


async def test_ui_advises_transliteration_and_never_lowercasing(client_ready):
    """The engine retries a phrase lowercased itself, so only the alphabet is left."""
    js = (await client_ready.get("/static/app.js")).text
    assert "опенвиспр вместо OpenWhispr" in js
    assert "Пишите строчными" not in js


async def test_sse_query_key_accepted(client_ready_with_key):
    response = await client_ready_with_key.get("/api/status", params={"api_key": "secret"})
    assert response.status_code == 200


async def test_glossary_is_a_field_of_chips_with_import_and_export(client_ready):
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    for marker in ["btn-export", "glossary-file", "btn-add", "add-phrase", "tag-list"]:
        assert marker in html
    assert "<textarea" not in html  # список правится чипами, а не текстовым полем
    assert "importGlossary" in js and "exportGlossary" in js and "addPhrases" in js
    assert "глоссарий.txt" in js


async def test_a_phrase_already_in_the_list_is_not_added_twice(client_ready):
    """Сравнение как у движка: регистр и «ё» не делают фразу новой."""
    js = (await client_ready.get("/static/app.js")).text
    assert 'toLowerCase().replace(/ё/g, "е")' in js
    assert "уже в списке" in js and "flashChip" in js


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
    assert "файлов за всё время" in html
    assert "renderRecent" in js and "avg_elapsed_total" in js


async def test_microphone_button_explains_itself_without_https(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    assert "window.isSecureContext" in js
    assert "только на localhost или по HTTPS" in js


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


async def test_deploy_progress_lives_on_the_head_card(client_ready):
    """Один индикатор вместо двух: взгляд там, где нажали, а не в шапке страницы."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    css = (await client_ready.get("/static/style.css")).text
    assert "progress-bar" not in html  # верхнего индикатора больше нет
    assert "renderDeploying" in js and "status.deploying" in js
    assert "head-progress" in css and 'data-indeterminate' in css
    for phase in ("скачиваю", "запускаю движок", "собираю граф"):
        assert phase in js


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
    assert "ROLLBACK.test(status.detail" in js


async def test_connection_section_links_api_docs_and_models(client_ready):
    """Схема управляющего API и список моделей - две ссылки, за которыми чаще всего
    идут в чужую документацию вместо своей консоли."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert 'id="link-docs"' in html and 'id="snip-models"' in html
    assert "/api/docs" in js and "/v1/models" in js


async def test_ui_offers_window_concurrency_and_names_its_condition(client_ready):
    """Селект бесполезен без пула 2: движок берет свободный слот без ожидания, и при
    пуле 1 параллельность молча ничего не делает. Условие должно стоять в интерфейсе."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    assert "opt-window-concurrency" in html
    assert "пул" in html.lower()
    assert "file_window_concurrency" in js
