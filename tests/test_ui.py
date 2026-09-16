import re

from conftest import route_paths


async def test_index_served_with_expected_sections(client_ready):
    """Разметка по-английски: это язык по умолчанию, русский включается кнопкой."""
    html = (await client_ready.get("/")).text
    for marker in ["Status", "Models", "Check", "Glossary", "Connecting", "Logs"]:
        assert marker in html


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


async def test_deploy_progress_lives_on_the_head_card(client_ready):
    """Один индикатор вместо двух: взгляд там, где нажали, а не в шапке страницы."""
    html = (await client_ready.get("/")).text
    js = (await client_ready.get("/static/app.js")).text
    css = (await client_ready.get("/static/style.css")).text
    assert "progress-bar" not in html  # верхнего индикатора больше нет
    assert "renderDeploying" in js and "status.deploying" in js
    assert "head-progress" in css and 'data-indeterminate' in css
    texts = (await client_ready.get("/static/i18n.js")).text
    for phase in ("скачиваю", "запускаю движок", "собираю граф"):
        assert phase in texts
    for phase in ("downloading", "starting the engine", "building the graph"):
        assert phase in texts


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
    assert "нужен пул 2" in texts and "needs pool 2" in texts
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
    assert by_id["e2e_rnnt"]["badge"] == "best for Russian"
    ru_by_id = {head["id"]: head for head in russian["heads"]}
    assert ru_by_id["e2e_rnnt"]["badge"] == "лучшая для русского"
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
