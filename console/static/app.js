/* Консоль STT: без сборки и без зависимостей.
   Запись с микрофона собирается в WAV прямо в браузере, поэтому в образе не нужен ffmpeg. */

const $ = (id) => document.getElementById(id);
const KEY_STORAGE = "stt_console_key";

const state = {
  key: localStorage.getItem(KEY_STORAGE) || "",
  status: null,
  /* Карточки голов строятся один раз и обновляются на месте: пересборка на каждое
     событие развертывания и была тем морганием, из-за которого прогресс терялся. */
  heads: new Map(),
  recording: null,
};

/* ---------------------------------------------------------------- транспорт */

function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  if (state.key) headers["Authorization"] = "Bearer " + state.key;
  return headers;
}

async function api(path, options, retry) {
  const opts = Object.assign({}, options || {});
  opts.headers = authHeaders(opts.headers);
  const response = await fetch(path, opts);
  if (response.status === 401 && !retry) {
    const entered = window.prompt("Сервис защищён ключом. Введите API_KEY из файла .env:", "");
    if (entered) {
      state.key = entered.trim();
      localStorage.setItem(KEY_STORAGE, state.key);
      renderSnippets();
      return api(path, options, true);
    }
  }
  if (!response.ok) {
    let message = "Ошибка " + response.status;
    try {
      const body = await response.json();
      if (body && body.error && body.error.message) message = body.error.message;
      else if (body && body.detail) message = JSON.stringify(body.detail);
    } catch (err) {
      /* тело не JSON — оставляем код */
    }
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.indexOf("application/json") === 0 ? response.json() : response.text();
}

/* ------------------------------------------------------------------- статус */

const STATE_WORDS = {
  stopped: "не развёрнуто",
  downloading: "скачиваю модель",
  starting: "запускаю движок",
  ready: "готово",
  error: "ошибка",
};

function renderStatus(status) {
  state.status = status;
  const pill = $("state-pill");
  pill.dataset.state = status.status;
  $("state-label").textContent = STATE_WORDS[status.status] || status.status;
  $("state-detail").textContent = status.detail || "—";
  $("btn-stop").classList.toggle("hidden", status.status !== "ready");
  $("key-badge").classList.toggle("hidden", !status.api_key_set);

  const engine = status.engine || {};
  const metrics = status.metrics || {};
  $("ro-variant").textContent = engine.variant || "—";
  $("ro-elapsed").textContent = metrics.avg_elapsed !== null && metrics.avg_elapsed !== undefined
    ? metrics.avg_elapsed.toFixed(2) + " с" : "—";
  $("ro-rtf").textContent = metrics.avg_rtf ? "×" + (1 / metrics.avg_rtf).toFixed(1) : "—";
  $("ro-total").textContent = metrics.total_files === undefined ? "—" : metrics.total_files;
  $("ro-restarts").textContent = status.restart_count === undefined ? "—" : status.restart_count;
  $("ro-uptime").textContent = formatUptime(status.uptime_seconds);
  renderRecent(metrics);

  if (status.defaults) applyDefaults(status.defaults);
  renderEnvDiff(status.env);
  renderDeploying(status);
  afterStatusSettles(status.status);
}

/* Занятость берется от сервера, а не от POST: `/api/deploy` отвечает 202 сразу, а
   развертывание идет еще секунды или минуты. */
const deployBusy = (name) => name === "downloading" || name === "starting";

/* Развертывание кончилось - карточки и глоссарий перечитываются ровно один раз: голова
   сменилась, значит сменились и «скачано», и словарь, по которому считаются отброшенные
   фразы. Внутри развертывания перечитывать нечего, там всё ведет renderDeploying. */
let settledStatus = null;
function afterStatusSettles(name) {
  const wasBusy = deployBusy(settledStatus);
  settledStatus = name;
  if (!wasBusy || deployBusy(name)) return;
  loadHeads();
  loadGlossary(true);
}

/* Состояние сильнее .env намеренно: иначе выбранная здесь голова откатывалась бы к
   значению из файла после каждого перезапуска контейнера. Поэтому расхождение не
   сливается само, а показывается - и применяется одной кнопкой. */

const FIELD_WORDS = {
  variant: "голова", punctuation: "пунктуация", itn: "числа цифрами", vad: "пропуск тишины",
  pool_size: "одновременных распознаваний", hotwords_boost: "сила подсказки",
  hotwords_default: "словарь брендов",
};
const humanValue = (v) => (v === true ? "включен" : v === false ? "выключен" : String(v));

function renderEnvDiff(env) {
  const box = $("env-diff");
  const diverges = (env && env.diverges) || {};
  const names = Object.keys(diverges);
  box.classList.toggle("hidden", !names.length);
  if (!names.length) return;
  const list = $("env-diff-list");
  list.textContent = "";
  names.forEach((name) => {
    const li = document.createElement("li");
    li.className = "mono";
    li.textContent = (FIELD_WORDS[name] || name) + ": в .env " + humanValue(diverges[name].env) +
      ", развёрнуто " + humanValue(diverges[name].state);
    list.appendChild(li);
  });
  $("btn-deploy-env").disabled = deployBusy(state.status && state.status.status);
}

async function deployFromEnv() {
  const env = state.status && state.status.env && state.status.env.config;
  if (!env) return;
  // Форма перезаполняется значениями .env, дальше обычный путь развёртывания.
  $("opt-punctuation").value = env.punctuation;
  $("opt-itn").value = env.itn;
  $("opt-vad").value = String(env.vad);
  setSelectValue($("opt-pool"), String(env.pool_size));
  $("opt-hotwords-default").value = String(env.hotwords_default);
  $("opt-boost").value = String(env.hotwords_boost);
  await deploy(env.variant);
}

/* POOL_SIZE в .env допускает до 8, а в разметке перечислены 1-4: значение, которого
   в списке нет, пришлось бы молча проигнорировать. */
function setSelectValue(select, value) {
  if (![...select.options].some((option) => option.value === value)) {
    select.add(new Option(value, value));
  }
  select.value = value;
}

/* Последние файлы: по ним видно не «в среднем хорошо», а что именно тормозило.
   Средняя задержка за всё время идёт рядом — окно в 20 запросов её не покажет. */
const RECENT_SHOWN = 4;

function renderRecent(metrics) {
  const recent = (metrics.recent || []).slice(0, RECENT_SHOWN);
  $("recent").classList.toggle("hidden", !recent.length);
  if (!recent.length) return;

  const rows = $("recent-rows");
  rows.textContent = "";
  recent.forEach((item) => {
    const tr = document.createElement("tr");
    tr.appendChild(cell(item.name, "name"));
    tr.appendChild(cell(item.audio_seconds ? item.audio_seconds.toFixed(1) + " с" : "—"));
    tr.appendChild(cell(item.elapsed.toFixed(2) + " с"));
    tr.appendChild(cell(item.rtf ? "×" + (1 / item.rtf).toFixed(1) : "—"));
    rows.appendChild(tr);
  });

  const total = metrics.avg_elapsed_total;
  $("recent-note").textContent = total
    ? "средняя задержка за всё время — " + total.toFixed(2) + " с на файл"
    : "";
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  td.title = text;
  return td;
}

function plural(count, one, few, many) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return count + " " + many;
  if (mod10 === 1) return count + " " + one;
  if (mod10 >= 2 && mod10 <= 4) return count + " " + few;
  return count + " " + many;
}

function phraseCount(count) {
  return count ? plural(count, "фраза", "фразы", "фраз") : "фраз нет";
}

function formatUptime(seconds) {
  if (seconds === undefined || seconds === null) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return hours + " ч " + minutes + " мин";
  if (minutes) return minutes + " мин";
  return seconds + " с";
}

let defaultsApplied = false;
function applyDefaults(defaults) {
  if (defaultsApplied) return;
  defaultsApplied = true;
  $("opt-punctuation").value = defaults.punctuation;
  $("opt-itn").value = defaults.itn;
  $("opt-vad").value = String(defaults.vad);
  setSelectValue($("opt-pool"), String(defaults.pool_size));
  $("opt-hotwords-default").value = String(defaults.hotwords_default);
  $("opt-boost").value = String(defaults.hotwords_boost);
}

async function refreshStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (err) {
    $("state-detail").textContent = err.message;
  }
}

/* ------------------------------------------------------------------- модели */

async function loadHeads() {
  try {
    renderHeads(await api("/api/models"));
  } catch (err) {
    /* каталог не критичен для остальной страницы */
  }
}

/* Карточка строится один раз, дальше меняются только её подписи: перестройка узлов и
   была тем морганием, из-за которого прогресс развёртывания терялся из виду. */
function renderHeads(payload) {
  const container = $("heads");
  payload.heads.forEach((head) => {
    let entry = state.heads.get(head.id);
    if (!entry) {
      entry = buildHeadCard(head);
      state.heads.set(head.id, entry);
      container.appendChild(entry.card);
    }
    entry.head = head;
    entry.card.dataset.deployed = String(head.deployed);
    entry.downloaded.classList.toggle("hidden", !head.downloaded);
    entry.button.textContent = headButtonLabel(head);
    entry.button.className = head.deployed ? "btn btn-quiet" : "btn btn-primary";
  });
  renderDeploying(state.status || {});
}

const headButtonLabel = (head) => (head.deployed ? "Перезапустить" : "Развернуть");

function buildHeadCard(head) {
  const card = document.createElement("article");
  card.className = "head";

  const title = document.createElement("h3");
  title.textContent = head.title;
  /* Значок - про наш собственный замер, а не про чужую таблицу, поэтому источник
     висит подсказкой прямо на нём. */
  if (head.badge) {
    const badge = document.createElement("span");
    badge.className = "head-badge";
    badge.textContent = head.badge;
    badge.title = head.badge_note || "";
    title.appendChild(badge);
  }
  const subtitle = document.createElement("p");
  subtitle.textContent = head.subtitle;

  const meta = document.createElement("div");
  meta.className = "head-meta";
  meta.appendChild(tag(head.languages.join(" · ")));
  meta.appendChild(tag("~" + head.size_mb + " МБ"));
  if (head.native_punctuation) meta.appendChild(tag("пунктуация в модели"));
  // Значок «скачано» создаётся всегда и прячется: иначе его появление пересобирало бы meta.
  const downloadedTag = tag("скачано", true);
  downloadedTag.classList.add("hidden");
  meta.appendChild(downloadedTag);

  const actions = document.createElement("div");
  actions.className = "head-actions";
  const button = document.createElement("button");
  button.type = "button";
  button.addEventListener("click", () => deploy(head.id));
  const progress = document.createElement("div");
  progress.className = "head-progress hidden";
  progress.innerHTML = '<span class="head-progress-bar"></span><span class="head-progress-text mono"></span>';
  actions.append(button, progress);

  const error = document.createElement("p");
  error.className = "head-error mono hidden";

  card.append(title, subtitle, meta, actions, error);
  return { card, button, progress, error, downloaded: downloadedTag, head };
}

/* Фаза развёртывания живёт на карточке цели: пользователь смотрит туда, где нажал.
   Процентов у проверки весов и сборки графа нет, поэтому полоса там бегущая. */

const SLOW_START_MS = 20000;
const ROLLBACK = /откат/i;

const PHASE_WORDS = {
  downloading: (s) => (s.download_percent === null || s.download_percent === undefined)
    ? "проверяю веса"
    : "скачиваю " + s.download_percent + "%",
  starting: (s) => {
    if (ROLLBACK.test(s.detail || "")) return "откат на прежнюю голову";
    return startingSince && Date.now() - startingSince > SLOW_START_MS
      ? "собираю граф, первый запуск до 2 минут"
      : "запускаю движок";
  },
};

// `lastTarget` - то, что разворачивается прямо сейчас (при откате оно меняется на
// прежнюю голову), `clickedTarget` - то, что нажали: приговор нужен именно там.
let lastTarget = null;
let clickedTarget = null;
let deployingSeries = false;
let startingSince = 0;
// Жалоба держится на карточке до следующей попытки: сервер уже через секунду отвечает
// «готово» или «остановлено», и без этого текст ошибки исчезал бы раньше, чем прочтут.
let cardError = null;

function renderDeploying(status) {
  const active = deployBusy(status.status);
  const target = active ? status.deploying || null : null;
  if (status.status !== "starting" || target !== lastTarget) startingSince = 0;
  if (status.status === "starting" && !startingSince) startingSince = Date.now();
  if (active) {
    if (!deployingSeries) clickedTarget = target;
    deployingSeries = true;
    lastTarget = target;
    cardError = null;
  } else {
    deployingSeries = false;
    if (status.status === "error" && lastTarget) {
      cardError = { id: lastTarget, text: status.detail || "не удалось развернуть" };
    } else if (status.status === "ready" && clickedTarget && ROLLBACK.test(status.detail || "")) {
      // Откат кончается зелёным `ready` на другой голове, и без этой ветки о неудаче
      // говорила бы только пилюля наверху - ровно то, что мы и убирали.
      cardError = { id: clickedTarget, text: status.detail };
    }
  }

  state.heads.forEach((entry, id) => {
    const isTarget = active && id === target;
    const failed = cardError !== null && cardError.id === id;
    entry.button.classList.toggle("hidden", isTarget);
    entry.button.disabled = active;
    entry.button.textContent = failed ? "Повторить" : headButtonLabel(entry.head);
    entry.progress.classList.toggle("hidden", !isTarget);
    entry.error.classList.toggle("hidden", !failed);
    if (failed) entry.error.textContent = cardError.text;
    if (!isTarget) return;
    const percent = status.status === "downloading" ? status.download_percent : null;
    const unknown = percent === null || percent === undefined;
    entry.progress.dataset.indeterminate = String(unknown);
    entry.progress.style.setProperty("--pct", (unknown ? 0 : percent) + "%");
    entry.progress.querySelector(".head-progress-text").textContent = PHASE_WORDS[status.status](status);
    entry.progress.title = status.detail || "";
  });
}

function tag(text, on) {
  const span = document.createElement("span");
  if (on) span.className = "on";
  span.textContent = text;
  return span;
}

async function deploy(variant) {
  // Карточка отвечает до первого события с сервера: между нажатием и SSE проходит
  // заметная доля секунды, и молчание в этот момент читается как «кнопка не сработала».
  clickedTarget = variant;
  renderDeploying({ status: "downloading", deploying: variant, download_percent: null, detail: "" });
  // Пустое или нечисловое поле силы подсказки — не ноль: Number("") дал бы 0, то есть
  // «подсказка выключена», чего пользователь не просил. undefined исчезает из JSON, и
  // сервер берёт значение из своих defaults.
  const boost = parseFloat($("opt-boost").value);
  try {
    await api("/api/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        variant: variant,
        punctuation: $("opt-punctuation").value,
        itn: $("opt-itn").value,
        vad: $("opt-vad").value === "true",
        pool_size: Number($("opt-pool").value),
        hotwords_default: $("opt-hotwords-default").value === "true",
        hotwords_boost: Number.isFinite(boost) ? boost : undefined,
      }),
    });
  } catch (err) {
    // 401 или 422 - ответ про эту голову, поэтому и показывается на её карточке.
    $("state-detail").textContent = err.message;
    cardError = { id: variant, text: err.message };
    await refreshStatus();
    return;
  }
  // Дальше карточку ведут SSE и опрос статуса: другая голова - другой словарь, но
  // пересчитывать его до готовности движка нечем.
  await refreshStatus();
}

async function stopEngine() {
  try {
    await api("/api/stop", { method: "POST" });
  } catch (err) {
    $("state-detail").textContent = err.message;
  }
  await refreshStatus();
  await loadHeads();
}

/* ---------------------------------------------------------------- глоссарий */

/* Фраза здесь — не тег, а слово, которому учат движок, поэтому чип несёт её судьбу:
   ту, что словарь развёрнутой головы написать не может, движок молча выбросит, и она
   показана зачёркнутой. Отдельного списка отброшенных больше нет — он и есть список. */

const glossary = { entries: [], applied: "", dropped: new Set(), envMissing: [] };

const fold = (phrase) => phrase.toLowerCase().replace(/ё/g, "е");

async function loadGlossary(keepEdits) {
  try {
    const payload = await api("/api/glossary");
    glossary.dropped = new Set((payload.dropped || []).map(fold));
    if (!keepEdits) {
      glossary.entries = parseGlossary(payload.text);
      glossary.applied = glossaryText();
    }
    renderGlossary(payload);
  } catch (err) {
    /* не критично для остальной страницы */
  }
}

function parseGlossary(text) {
  return (text || "")
    .split(/[\n,]/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => {
      const [phrase, weight] = chunk.split("|").map((part) => part.trim());
      return { phrase, weight: weight || "" };
    })
    .filter((entry) => entry.phrase);
}

function glossaryText() {
  return glossary.entries
    .map((entry) => (entry.weight ? entry.phrase + "|" + entry.weight : entry.phrase))
    .join("\n");
}

function renderGlossary(payload) {
  const list = $("tag-list");
  list.textContent = "";
  glossary.entries.forEach((entry, index) => list.appendChild(tagChip(entry, index)));

  $("glossary-count").textContent = phraseCount(glossary.entries.length);
  $("btn-glossary").disabled = glossaryText() === glossary.applied;
  renderReach(payload);
  renderEnvMissing(payload);
}

/* INITIAL_CONTEXT читается один раз, при создании файла глоссария, — дальше правки в
   консоли сильнее. Автоматически доливать фразы из .env нельзя: удалённая здесь фраза
   не должна возвращаться после перезапуска. Поэтому список показывается, а решает
   пользователь. Ответ сервера кэшируется: правка списка перерисовывает чипы без
   payload, и подсказка иначе исчезала бы до следующей загрузки. */

function renderEnvMissing(payload) {
  if (payload && payload.env_missing) glossary.envMissing = payload.env_missing;
  const missing = (glossary.envMissing || []).filter(
    (phrase) => !glossary.entries.some((entry) => fold(entry.phrase) === fold(phrase)));
  $("glossary-env").classList.toggle("hidden", !missing.length);
  if (!missing.length) return;
  $("glossary-env-text").textContent = "В .env (INITIAL_CONTEXT) есть " +
    plural(missing.length, "фраза", "фразы", "фраз") + ", которых нет в списке: " +
    missing.join(", ") + ".";
  $("btn-glossary-env").onclick = () => addPhrases(missing.join("\n"));
}

function tagChip(entry, index) {
  const chip = document.createElement("span");
  chip.className = "tag";
  chip.dataset.index = String(index);
  if (glossary.dropped.has(fold(entry.phrase))) {
    chip.dataset.dropped = "true";
    chip.title = "движок не сможет это написать и выбросит фразу";
  }

  const label = document.createElement("span");
  label.className = "tag-text";
  label.textContent = entry.phrase;
  chip.appendChild(label);

  if (entry.weight) {
    const weight = document.createElement("span");
    weight.className = "tag-weight";
    weight.textContent = "·" + entry.weight;
    weight.title = "вес подсказки";
    chip.appendChild(weight);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "tag-remove";
  remove.textContent = "×";
  remove.setAttribute("aria-label", "Убрать «" + entry.phrase + "»");
  remove.addEventListener("click", () => removePhrase(index));
  chip.appendChild(remove);
  return chip;
}

function renderReach(payload) {
  const reach = $("glossary-reach");
  const hint = $("dropped-hint");
  const total = glossary.entries.length;
  const dropped = glossary.dropped.size;
  const head = payload && payload.variant ? "головы " + payload.variant : "выбранной головы";

  if (!total) {
    reach.textContent = "";
    hint.textContent = "";
    return;
  }
  if (payload && payload.usable_count === null) {
    reach.dataset.loss = "false";
    reach.textContent = "веса ещё не скачаны — что дойдёт до движка, будет видно после развёртывания";
    hint.textContent = "";
    return;
  }
  reach.dataset.loss = String(dropped > 0);
  reach.textContent = dropped
    ? "зачёркнутое движок выбросит: " + plural(dropped, "фраза", "фразы", "фраз") + " из " + total
    : "все фразы дойдут до движка" + (payload && payload.approximate ? "; оценка приблизительная" : "");
  hint.textContent = dropped ? droppedHint(payload && payload.alphabet, head) : "";
}

/* Подсказка собирается из того, что в словаре на диске, а не из общего правила:
   для e2e_rnnt заглавные и латиница есть, для rnnt нет ни того, ни другого.
   Про регистр советовать нечего — движок сам пробует фразу строчными и с «е»
   вместо «ё», поэтому уцелеть тут может только другой алфавит. */
function droppedHint(alphabet, head) {
  if (!alphabet) return "В словаре " + head + " нет части этих символов.";

  const missing = [];
  if (!alphabet.latin) missing.push("латиницы");
  if (!alphabet.digits) missing.push("цифр");
  if (!missing.length) {
    return "Эти фразы содержат символы, которых нет в словаре " + head + ".";
  }
  const advice = alphabet.latin
    ? ""
    : " Пишите такие названия по-русски: опенвиспр вместо OpenWhispr.";
  return "В словаре " + head + " нет " + missing.join(" и ") + "." + advice;
}

/* --- правка списка --- */

function addPhrases(raw) {
  const added = [];
  let duplicate = null;
  parseGlossary(raw).forEach((entry) => {
    const twin = glossary.entries.findIndex((item) => fold(item.phrase) === fold(entry.phrase));
    if (twin >= 0) {
      duplicate = twin;
      return;
    }
    glossary.entries.push(entry);
    added.push(entry.phrase);
  });

  renderGlossary(null);
  loadGlossary(true);
  if (added.length) {
    $("glossary-hint").textContent = "добавлено, нажмите «Применить»";
  } else if (duplicate !== null) {
    $("glossary-hint").textContent = "уже в списке";
    flashChip(duplicate);
  }
  return added.length;
}

function flashChip(index) {
  const chip = $("tag-list").querySelector('[data-index="' + index + '"]');
  if (!chip) return;
  chip.scrollIntoView({ block: "nearest", behavior: "smooth" });
  chip.dataset.flash = "true";
  setTimeout(() => delete chip.dataset.flash, 900);
}

function removePhrase(index) {
  glossary.entries.splice(index, 1);
  renderGlossary(null);
  loadGlossary(true);
  $("glossary-hint").textContent = "убрано, нажмите «Применить»";
}

function commitInput() {
  const input = $("add-phrase");
  if (!input.value.trim()) return;
  addPhrases(input.value);
  input.value = "";
}

/* --- отправка на сервер --- */

/* Форматирование берёт на себя сервер: разберёт запятые, веса после вертикальной
   черты и повторы, а вернёт канонический вид, которым мы и заменяем свой список. */
async function sendGlossary(text, note) {
  const hint = $("glossary-hint");
  hint.textContent = "применяю…";
  try {
    const payload = await api("/api/glossary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    hint.textContent = payload.applied
      ? note || "движок перечитал глоссарий"
      : "не удалось применить";
    await loadGlossary();
    return payload.count;
  } catch (err) {
    hint.textContent = err.message;
    return null;
  }
}

function applyGlossary() {
  return sendGlossary(glossaryText());
}

/* Импорт дописывает к тому, что есть: повторы отсеются, а стереть собранный руками
   список одним неверным файлом было бы обидно. */
async function importGlossary(file) {
  const before = glossary.entries.length;
  const added = addPhrases(await file.text());
  const skipped = parseGlossary(await file.text()).length - added;
  $("glossary-hint").textContent = added
    ? "из файла добавлено " + plural(added, "фраза", "фразы", "фраз") +
      (skipped ? ", повторов пропущено " + skipped : "") + " — нажмите «Применить»"
    : "новых фраз в файле не нашлось";
  return glossary.entries.length - before;
}

function exportGlossary() {
  if (!glossary.entries.length) {
    $("glossary-hint").textContent = "глоссарий пуст";
    return;
  }
  const url = URL.createObjectURL(
    new Blob([glossaryText() + "\n"], { type: "text/plain;charset=utf-8" })
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "глоссарий.txt";
  link.click();
  URL.revokeObjectURL(url);
  $("glossary-hint").textContent = "файл сохранён";
}

/* ----------------------------------------------------------------- проверка */

async function sendAudio(blob, filename) {
  const result = $("result");
  const text = $("result-text");
  result.classList.remove("hidden");
  text.textContent = "распознаю…";
  const form = new FormData();
  form.append("file", blob, filename);
  try {
    const payload = await api("/api/test", { method: "POST", body: form });
    text.textContent = payload.text || "(пусто — тишина или слишком короткая запись)";
    renderLatency(payload);
  } catch (err) {
    text.textContent = err.message;
    renderLatency(null);
  }
  refreshStatus();
}

function renderLatency(payload) {
  const verdict = $("latency-verdict");
  if (!payload) {
    $("bar-audio").style.width = "0";
    $("bar-work").style.width = "0";
    $("val-audio").textContent = "—";
    $("val-work").textContent = "—";
    verdict.textContent = "";
    return;
  }
  const audio = payload.audio_seconds || 0;
  const work = payload.elapsed || 0;
  const scale = Math.max(audio, work, 0.001);
  $("bar-audio").style.width = (audio / scale) * 100 + "%";
  $("bar-work").style.width = (work / scale) * 100 + "%";
  $("val-audio").textContent = audio ? audio.toFixed(1) + " с" : "неизвестно";
  $("val-work").textContent = work.toFixed(2) + " с";
  if (!audio) {
    verdict.textContent = "длительность известна только для WAV и WebM";
    verdict.dataset.slow = "false";
    return;
  }
  const ratio = audio / work;
  verdict.dataset.slow = String(ratio < 1);
  verdict.textContent = ratio >= 1
    ? "быстрее реального времени в " + ratio.toFixed(1) + " раза"
    : "медленнее записи в " + (1 / ratio).toFixed(1) + " раза";
}

/* --------------------------------------------------- запись с микрофона → WAV */

const TARGET_RATE = 16000;

const WORKLET_SOURCE = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor('capture', Capture);
`;

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx({ sampleRate: TARGET_RATE });
  const source = ctx.createMediaStreamSource(stream);
  const chunks = [];
  const levels = [];

  const onChunk = (data) => {
    chunks.push(data);
    let peak = 0;
    for (let i = 0; i < data.length; i += 1) peak = Math.max(peak, Math.abs(data[i]));
    levels.push(peak);
    if (levels.length > 120) levels.shift();
    drawMeter(levels);
  };

  let node;
  if (ctx.audioWorklet) {
    const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
    await ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    node = new AudioWorkletNode(ctx, "capture");
    node.port.onmessage = (event) => onChunk(event.data);
  } else {
    node = ctx.createScriptProcessor(2048, 1, 1);
    node.onaudioprocess = (event) => onChunk(new Float32Array(event.inputBuffer.getChannelData(0)));
  }
  source.connect(node);
  if (!ctx.audioWorklet) node.connect(ctx.destination);

  $("meter").classList.remove("hidden");
  state.recording = { stream, ctx, node, source, chunks };
}

async function stopRecording() {
  const rec = state.recording;
  state.recording = null;
  if (!rec) return;
  rec.source.disconnect();
  rec.node.disconnect();
  rec.stream.getTracks().forEach((track) => track.stop());
  const rate = rec.ctx.sampleRate;
  await rec.ctx.close();
  drawMeter([]);
  $("meter").classList.add("hidden");

  const samples = concat(rec.chunks);
  if (samples.length < rate * 0.25) {
    $("result").classList.remove("hidden");
    $("result-text").textContent = "Запись слишком короткая — скажите фразу подольше.";
    renderLatency(null);
    return;
  }
  const wav = encodeWav(resample(samples, rate, TARGET_RATE), TARGET_RATE);
  sendAudio(wav, "запись.wav");
}

function concat(chunks) {
  let total = 0;
  chunks.forEach((chunk) => { total += chunk.length; });
  const out = new Float32Array(total);
  let offset = 0;
  chunks.forEach((chunk) => { out.set(chunk, offset); offset += chunk.length; });
  return out;
}

function resample(samples, from, to) {
  if (from === to) return samples;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, samples.length - 1);
    const weight = position - left;
    out[i] = samples[left] * (1 - weight) + samples[right] * weight;
  }
  return out;
}

function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return new Blob([view], { type: "audio/wav" });
}

function drawMeter(levels) {
  const canvas = $("meter");
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  const styles = getComputedStyle(document.body);
  ctx.fillStyle = styles.getPropertyValue("--amber").trim() || "#e8a33d";
  const barWidth = 3;
  const gap = 1;
  const count = Math.floor(width / (barWidth + gap));
  const shown = levels.slice(-count);
  shown.forEach((level, index) => {
    const bar = Math.max(2, Math.min(height, level * height * 1.8));
    ctx.fillRect(index * (barWidth + gap), (height - bar) / 2, barWidth, bar);
  });
}

async function toggleRecording() {
  const button = $("btn-record");
  if (state.recording) {
    button.textContent = "Записать с микрофона";
    button.classList.remove("btn-record-active");
    await stopRecording();
    return;
  }
  try {
    await startRecording();
    button.textContent = "Остановить запись";
    button.classList.add("btn-record-active");
  } catch (err) {
    $("result").classList.remove("hidden");
    $("result-text").textContent =
      "Микрофон недоступен: " + err.message +
      ". Браузеры разрешают запись только на localhost или по HTTPS.";
  }
}

/* --------------------------------------------------------------- сниппеты */

function renderSnippets() {
  const origin = window.location.origin;
  const auth = state.key ? ' \\\n  -H "Authorization: Bearer ' + state.key + '"' : "";
  $("snip-curl").textContent =
    "curl -X POST " + origin + "/v1/audio/transcriptions" + auth + " \\\n" +
    "  -F model=whisper-1 \\\n" +
    "  -F file=@запись.wav";
  $("snip-py").textContent =
    "from openai import OpenAI\n\n" +
    'client = OpenAI(base_url="' + origin + '/v1", api_key="' + (state.key || "не-нужен") + '")\n' +
    'with open("запись.wav", "rb") as audio:\n' +
    "    result = client.audio.transcriptions.create(model=\"whisper-1\", file=audio)\n" +
    "print(result.text)";
}

/* ------------------------------------------------------------------- логи */

function appendLog(line) {
  const logs = $("logs");
  logs.textContent += (logs.textContent ? "\n" : "") + line;
  const lines = logs.textContent.split("\n");
  if (lines.length > 400) logs.textContent = lines.slice(-400).join("\n");
  if ($("log-follow").checked) logs.scrollTop = logs.scrollHeight;
}

function connectEvents() {
  const query = state.key ? "?api_key=" + encodeURIComponent(state.key) : "";
  const source = new EventSource("/api/events" + query);
  source.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (payload.type === "log") {
      appendLog(payload.line);
      return;
    }
    if (payload.type === "snapshot") {
      renderStatus(payload);
      loadHeads();
      return;
    }
    if (payload.type === "status") {
      // Карточки не пересобираются: renderStatus сам перечитает их, когда развёртывание
      // закончится, а до тех пор меняется только фаза на карточке цели.
      refreshStatus();
      return;
    }
    if (payload.type === "download") {
      const status = Object.assign({}, state.status || {}, {
        status: "downloading",
        deploying: payload.variant,
        download_percent: payload.percent,
      });
      renderStatus(status);
    }
  };
  source.onerror = () => {
    source.close();
    setTimeout(connectEvents, 3000);
  };
}

/* -------------------------------------------------------------------- init */

function bindDropZone() {
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((name) =>
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    }));
  ["dragleave", "drop"].forEach((name) =>
    drop.addEventListener(name, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) sendAudio(file, file.name);
  });
}

function init() {
  /* Микрофон браузер отдаёт только в защищённом контексте. Сказать об этом заранее
     честнее, чем дать нажать кнопку и показать ошибку: причина не в сервисе. */
  if (!window.isSecureContext) {
    const button = $("btn-record");
    button.disabled = true;
    button.title = "Браузер разрешает запись только на localhost или по HTTPS";
    $("drop").textContent =
      "Перетащите сюда аудиофайл — запись с микрофона доступна только на localhost или по HTTPS";
  }
  $("btn-record").addEventListener("click", toggleRecording);
  $("btn-stop").addEventListener("click", stopEngine);
  $("btn-glossary").addEventListener("click", applyGlossary);
  $("btn-add").addEventListener("click", commitInput);
  $("tags").addEventListener("mousedown", (event) => {
    if (event.target === $("tags") || event.target === $("tag-list")) {
      event.preventDefault();
      $("add-phrase").focus();
    }
  });
  $("add-phrase").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      commitInput();
      return;
    }
    // Пустой ввод и Backspace — убрать последнюю фразу: привычно для полей с чипами.
    if (event.key === "Backspace" && !event.target.value && glossary.entries.length) {
      removePhrase(glossary.entries.length - 1);
    }
  });
  $("add-phrase").addEventListener("paste", (event) => {
    const text = (event.clipboardData || window.clipboardData).getData("text");
    if (text && /[\n,]/.test(text)) {
      event.preventDefault();
      addPhrases(text);
      event.target.value = "";
    }
  });
  $("btn-deploy-env").addEventListener("click", deployFromEnv);
  $("btn-export").addEventListener("click", exportGlossary);
  $("glossary-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) importGlossary(file);
    event.target.value = "";
  });
  $("file-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) sendAudio(file, file.name);
  });
  document.querySelectorAll(".copy").forEach((button) =>
    button.addEventListener("click", () => {
      navigator.clipboard.writeText($(button.dataset.target).textContent);
      button.textContent = "Скопировано";
      setTimeout(() => { button.textContent = "Скопировать"; }, 1500);
    }));
  bindDropZone();
  renderSnippets();
  refreshStatus();
  loadHeads();
  loadGlossary();
  connectEvents();
  setInterval(refreshStatus, 5000);
}

init();
