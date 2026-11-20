/* Консоль STT: без сборки и без зависимостей.
   Запись с микрофона собирается в WAV прямо в браузере, поэтому в образе не нужен ffmpeg.

   Все подписи идут через `t()` из i18n.js: язык интерфейса переключается на месте,
   без перезагрузки страницы, поэтому готовых строк здесь не остается. */

const $ = (id) => document.getElementById(id);
function setText(id, value) {
  const target = $(id);
  if (target.textContent !== value) target.textContent = value;
}
const KEY_STORAGE = "stt_console_key";
const { t, currentLang, setLang, applyStaticTexts } = window.i18n;

const state = {
  mode: null, csrf: "", key: "", leaving: false,
  status: null,
  heads: new Map(),
  catalog: "loading",
  selected: null,
  configDirty: false,

  recording: null,
};

/* ---------------------------------------------------------------- транспорт */

function authHeaders(extra, method = "GET") {
  const headers = Object.assign({}, extra || {});
  if (state.mode === "local" && state.key) headers.Authorization = "Bearer " + state.key;
  if (state.mode === "owner" && !["GET", "HEAD"].includes(method.toUpperCase())) headers["X-CSRF-Token"] = state.csrf;
  return headers;
}

function returnToLogin() {
  if (state.leaving) return;
  state.leaving = true; state.csrf = "";
  clearTimeout(reconnectTimer); eventSource?.close(); ownerAccess.clear();
  document.querySelector(".shell").hidden = true;
  location.replace("/login");
}

/* Текст сохранённой ошибки зависит от текущего языка, а не от языка запроса. */
class LocalizedError extends Error {
  constructor(key, params) { super(); this.key = key; this.params = params; }
  get message() { return t(this.key, this.params); }
}

async function readSession(signal) {
  const r = await fetch("/auth/session", {mode:"same-origin", credentials:"same-origin", redirect:"error", cache:"no-store", signal});
  if (!r.ok) throw new LocalizedError("auth.checkFailed");
  return r.json();
}
function acceptOwnerSession(session) {
  if (session.mode !== "owner") throw new LocalizedError("auth.checkFailed");
  if (session.authenticated === false) returnToLogin();
  if (state.leaving || session.authenticated !== true || typeof session.csrf_token !== "string" || !session.csrf_token) throw new LocalizedError("auth.checkFailed");
  state.csrf = session.csrf_token;
  state.jsonLimit = Number.isSafeInteger(session.json_limit_bytes) && session.json_limit_bytes > 0 ? session.json_limit_bytes : 1024 * 1024;
}

async function api(path, options, retry) {
  const opts = Object.assign({}, options || {});
  if (!state.mode || state.leaving) throw new LocalizedError("auth.unavailable");
  opts.headers = authHeaders(opts.headers, opts.method);
  const response = await fetch(path, {...opts, mode: "same-origin", credentials: "same-origin", redirect: "error", cache:"no-store"});
  if (response.status === 401 && state.mode === "owner") { returnToLogin(); throw new LocalizedError("auth.denied"); }
  if (response.status === 401 && state.mode === "local" && !retry && !state.keyAsked) {
    state.keyAsked = true;
    const entered = window.prompt(t("api.keyPrompt"), "");
    if (entered) {
      state.key = entered.trim();
      try { localStorage.setItem(KEY_STORAGE, state.key); } catch { /* Только в этой вкладке. */ }
      renderSnippets();
      return api(path, options, true);
    }
  }
  if (!response.ok) {
    let error = new LocalizedError("api.error", { status: response.status }), code;
    try {
      const body = await response.json();
      code = body?.error?.code;
      if (body?.error?.message) {
        const key = "error." + body.error.code;
        error = t(key) === key ? new Error(body.error.message) : new LocalizedError(key);
      }
      else if (body && body.detail) error = new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
    } catch (err) {
      /* тело не JSON — оставляем код */
    }
    // Только эта ошибка гарантирует, что сервер ещё не приступил к записи.
    if (!retry && state.mode === "owner" && opts.headers["X-CSRF-Token"] && response.status === 403 && code === "invalid_csrf") {
      opts.signal?.throwIfAborted();
      const session = await readSession(opts.signal);
      opts.signal?.throwIfAborted(); acceptOwnerSession(session);
      return api(path, options, true);
    }
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  return type.indexOf("application/json") === 0 ? response.json() : response.text();
}

/* ------------------------------------------------------------------- статус */

const stateWord = (name) => t("state." + name);

/* Сервер присылает и готовую строку (она же уходит в лог), и код с подстановками.
   Показываем код словами выбранного языка, а `detail` остаётся запасным вариантом:
   так новая фраза на сервере не превращается в пустую строку в интерфейсе. */
function detailText(status) {
  const code = status.detail_code;
  if (!code) return status.detail || "—";
  const params = Object.assign({}, status.detail_params || {});
  if (params.kind) params.reason = t("download." + params.kind);
  let text = t("detail." + code, params);
  if (text === "detail." + code) return status.detail || "—";
  if (code === "download.failed") {
    if (params.attempts > 1) text += t("detail.download.failedAttempts", params);
    if (params.running) text += t("detail.download.failedRunning", params);
  }
  return text;
}

function renderStatus(status) {
  state.status = status; state.statusError = "";
  const pill = $("state-pill");
  pill.dataset.state = status.status;
  $("state-label").textContent = stateWord(status.status) || status.status;
  $("state-detail").textContent = detailText(status);
  $("ro-state").textContent = stateWord(status.status);
  $("ro-target").textContent = [status.deploying, status.status === "downloading" && Number.isFinite(status.download_percent) ? status.download_percent + "%" : ""].filter(Boolean).join(" · ") || "—";
  renderStop();
  $("key-badge").classList.toggle("hidden", !status.api_key_set);

  const engine = status.engine || {};
  const metrics = status.metrics || {};
  $("ro-variant").textContent = engine.variant || "—";
  $("ro-elapsed").textContent = metrics.avg_elapsed !== null && metrics.avg_elapsed !== undefined
    ? t("unit.seconds", { value: metrics.avg_elapsed.toFixed(2) }) : "—";
  $("ro-rtf").textContent = metrics.avg_rtf ? "×" + (1 / metrics.avg_rtf).toFixed(1) : "—";
  $("ro-total").textContent = metrics.total_files === undefined ? "—" : metrics.total_files;
  $("ro-restarts").textContent = status.restart_count === undefined ? "—" : status.restart_count;
  $("ro-uptime").textContent = formatUptime(status.uptime_seconds);
  renderRecent(metrics);
  if (!state.keyOptionSet) { $("example-key").checked = state.mode === "owner" || !!status.api_key_set; state.keyOptionSet = true; renderSnippets(); }
  $("recent-empty").hidden = !!metrics.recent?.length;
  const values = (metrics.recent || []).map(r => r.elapsed).filter(Number.isFinite).sort((a,b) => a-b);
  const mid = values.length >> 1;
  const median = values.length % 2 ? values[mid] : (values[mid-1] + values[mid]) / 2;
  $("cur-median").textContent = values.length ? t("audio.sample", {value:median.toFixed(2), count:values.length}) : t("audio.noMetrics");
  renderCurrent(); renderAudio(); renderCompare();

  if (status.defaults) applyDefaults(status.defaults);
  renderEnvDiff(status.env);
  renderSelection();
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
  const prior = settledStatus;
  settledStatus = name;
  if (!prior || prior === name || deployBusy(name)) return;
  loadHeads();
  if (!glossary.saving) loadGlossary();
}

/* Состояние сильнее .env намеренно: иначе выбранная здесь голова откатывалась бы к
   значению из файла после каждого перезапуска контейнера. Поэтому расхождение не
   сливается само, а показывается - и применяется одной кнопкой. */

const fieldWord = (name) => t("field." + name);
const humanValue = (v) =>
  v === true ? t("env.value.on") : v === false ? t("env.value.off") : String(v);

function renderEnvDiff(env) {
  const box = $("env-diff");
  const diverges = (env && env.diverges) || {};
  const names = Object.keys(diverges);
  box.classList.toggle("hidden", !names.length && !envWindowConflict(env?.config));
  const list = $("env-diff-list");
  list.textContent = "";
  names.forEach((name) => {
    const li = document.createElement("li");
    li.className = "mono";
    li.textContent = t("env.line", {
      field: fieldWord(name),
      env: humanValue(diverges[name].env),
      state: humanValue(diverges[name].state),
    });
    list.appendChild(li);
  });
}

function envWindowConflict(cfg) {
  return cfg && cfg.file_window_concurrency > cfg.pool_size;
}
async function deployFromEnv() {
  const config = state.status?.env?.config;
  if (!config || envWindowConflict(config)) return;
  // Применяем точное содержимое .env, включая параметры, неактивные в редакторе.
  // До подтверждения сервера черновик формы остаётся нетронутым.
  await deploy(config, true);
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
    const name = cell(item.name, "name");
    if (["microphone", "file", "api"].includes(item.source)) name.append(el("small", t("source." + item.source), "source-kind"));
    tr.appendChild(name);
    tr.appendChild(cell(item.audio_seconds
      ? t("unit.seconds", { value: item.audio_seconds.toFixed(1) }) : "—"));
    tr.appendChild(cell(t("unit.seconds", { value: item.elapsed.toFixed(2) })));
    tr.appendChild(cell(item.rtf ? "×" + (1 / item.rtf).toFixed(1) : "—"));
    rows.appendChild(tr);
  });

  const total = metrics.avg_elapsed_total;
  $("recent-note").textContent = total
    ? t("recent.average", { value: total.toFixed(2) })
    : "";
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  td.title = text;
  return td;
}

/* Формы числа берёт Intl.PluralRules внутри i18n: в русском их три, в английском две. */
function phraseCount(count) {
  return count ? t("glossary.count", { count }) : t("glossary.none");
}

function formatUptime(seconds) {
  if (seconds === undefined || seconds === null) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return t("time.hours", { hours, minutes });
  if (minutes) return t("time.minutes", { minutes });
  return t("time.seconds", { seconds });
}

function applyDefaults(defaults) {
  if (!defaults?.variant) return;
  if (state.hydrated && state.configDirty) {
    if (!Object.entries(draftConfig()).every(([k,v]) => defaults[k] === v)) return;
    state.configDirty = false;
  }
  state.hydrated = true;
  state.selected = defaults.variant;
  $("opt-punctuation").value = defaults.punctuation;
  $("opt-itn").value = defaults.itn;
  $("opt-vad").value = String(defaults.vad);
  setSelectValue($("opt-pool"), String(defaults.pool_size));
  $("opt-hotwords-default").value = String(defaults.hotwords_default);
  $("opt-boost").value = String(defaults.hotwords_boost);
  setSelectValue($("opt-window-concurrency"), String(defaults.file_window_concurrency ?? 1));
}

async function refreshStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (err) {
    state.status = null; state.statusError = err;
    $("state-label").textContent = t("state.offline"); $("state-pill").dataset.state = "unknown";
    $("state-detail").textContent = err.message;
    for (const id of ["ro-state","ro-target","ro-variant","ro-elapsed","ro-rtf","ro-total","ro-restarts","ro-uptime","cur-median"]) $(id).textContent = "—";
    renderStop(); $("env-diff").classList.add("hidden"); $("key-badge").classList.add("hidden");
    $("recent").classList.add("hidden"); $("recent-empty").hidden = true;
    renderAudio(); renderCompare(); renderSelection(); renderCurrent();
  }
}

/* ------------------------------------------------------------------- модели */

const el = (name, text, cls) => {
  const node = document.createElement(name);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
let catalogRequest = 0;
async function loadHeads() {
  const request = ++catalogRequest;
  state.catalog = "loading"; renderCompare();
  try {
    const data = await api("/api/models?lang=" + encodeURIComponent(currentLang()));
    if (request !== catalogRequest) return;
    for (const head of data.heads) {
      let item = state.heads.get(head.id);
      if (!item) {
        const row = el("div", null, "modelrow"), radio = el("input"), label = el("label"), note = el("span", null, "modelstate");
        radio.type = "radio"; radio.name = "target-model"; radio.value = head.id; radio.id = "target-" + head.id;
        label.htmlFor = radio.id; label.append(head.title, el("code", head.id, "pill"));
        note.id = radio.id + "-state"; radio.setAttribute("aria-describedby", note.id);
        row.append(radio, label, note); $("heads").append(row);
        item = { radio, note }; state.heads.set(head.id, item);
      }
      item.head = head;
    }
    state.catalog = "ready"; renderSelection(); renderCompare(); renderCurrent();
  } catch (err) {
    if (request === catalogRequest) { state.catalog = "error"; $("head-note").textContent = err.message; renderCompare(); }
  }
}

function renderCurrent() {
  const engine = state.status?.engine;
  $("cur-model").textContent = !state.status ? t("audio.unknownModel") :
    state.heads.get(engine?.variant)?.head.title || engine?.variant || t("audio.noModel");
  $("cur-id").textContent = engine?.variant || "";
  for (const [id, name] of [["cur-punct", "punctuation"], ["cur-itn", "itn"]]) {
    $(id).textContent = engine?.[name] ? t("short." + engine[name]) : "—";
  }
}

function renderSelection() {
  const id = state.selected;
  const head = state.heads.get(id)?.head;
  for (const item of state.heads.values()) {
    item.radio.checked = item.head.id === id;
    const status = state.status;
    item.radio.disabled = !state.hydrated || !status?.defaults;
    const readiness = !status ? t("models.unknownState") : status.deploying === item.head.id && deployBusy(status.status)
      ? stateWord(status.status) : status.engine?.running && status.engine.variant === item.head.id
      ? t(status.status === "ready" ? "models.active" : "models.process") : "";
    item.note.textContent = [readiness,
      t(item.head.downloaded ? "head.downloaded" : "models.notDownloaded")].filter(Boolean).join(" · ");
  }
  $("cfg-model").textContent = head?.title || id || "—";
  if (head) {
    $("head-name").textContent = head.title;
    $("head-meta").replaceChildren(code(id), el("span",head.languages.join(" · ").toUpperCase()), el("span",t("head.size", { mb: head.size_mb })));
    $("head-description").textContent = head.subtitle;
    $("head-badge").textContent = head.badge || "";
    $("head-note").textContent = head.badge_note || "";
  }
  renderSettingHints();
  renderDeploying(state.status || {});
}

function renderSettingHints() {
  const pool = Number($("opt-pool").value) || 1, windows = $("opt-window-concurrency");
  for (const option of windows.options) option.disabled = Number(option.value) > pool;
  if (Number(windows.value) > pool) windows.value = String(pool);
  windows.disabled = pool === 1;
  const noHints = glossary.loaded && !glossary.applied && $("opt-hotwords-default").value === "false";
  $("opt-boost").disabled = noHints;
  for (const key of ["punctuation", "itn"]) {
    const auto = state.selected === "rnnt" ? "autoOn" : state.selected === "e2e_rnnt" ? "native" : "autoOff";
    setText(key + "-note", [t("opt." + key + ".note"), state.selected && t("opt." + key + "." + auto)].filter(Boolean).join(" "));
  }
  for (const key of ["vad", "pool", "dictionary"]) setText(key + "-note", t("opt." + key + ".note"));
  setText("window-note", t(windows.disabled ? "opt.window.locked" : "opt.window.note", {pool}));
  setText("boost-note", t(noHints ? "opt.boost.locked" : "opt.boost.note"));
}

function draftConfig() {
  return {variant: state.selected, punctuation: $("opt-punctuation").value, itn: $("opt-itn").value,
    vad: $("opt-vad").value === "true", pool_size: Number($("opt-pool").value),
    file_window_concurrency: Number($("opt-window-concurrency").value), ...(!$("opt-boost").disabled && {hotwords_boost: $("opt-boost").valueAsNumber}),
    hotwords_default: $("opt-hotwords-default").value === "true"};
}
let clickedTarget = null, deployPending = false, deployError = "", envError = null, rollbackStatus = null;
const isRollback = (status) => String(status.detail_code || "").indexOf("rollback.") === 0;
function renderDeploying(status) {
  const busy = deployBusy(status.status), progress = $("deploy-progress");
  $("config-editor").disabled = !state.hydrated || !status.defaults;
  $("btn-deploy").disabled = !status.defaults || !state.heads.has(state.selected) || busy || deployPending;
  const env = status.env?.config, conflict = envWindowConflict(env);
  $("btn-deploy-env").disabled = busy || deployPending || !env || !!conflict;
  setText("env-error", conflict ? t("env.windowsConflict", {windows:env.file_window_concurrency, pool:env.pool_size}) : envError?.message || "");
  $("btn-deploy").textContent = t(state.selected && state.selected === status.engine?.variant ? "head.redeploy" : "head.deploy");
  $("draft-note").textContent = !status.defaults ? state.statusError?.message || t("audio.awaitStatus") : state.configDirty ? t("settings.draft") : "";
  if (status.status === "ready" && clickedTarget && isRollback(status)) rollbackStatus = status;
  const phase = busy ? detailText(status) : "";
  const pct = status.status === "downloading" && Number.isFinite(status.download_percent) ? status.download_percent : null;
  progress.hidden = pct === null;
  if (pct !== null) progress.value = pct;
  const label = deployPending ? t("settings.wait") : busy ? [phase, status.deploying, pct !== null ? pct + "%" : ""].filter(Boolean).join(" · ")
    : deployError?.message || (rollbackStatus ? detailText(rollbackStatus) : clickedTarget && status.status ? detailText(status) : "");
  if ($("deploy-feedback").textContent !== label) $("deploy-feedback").textContent = label;
}
async function deploy(config, fromEnv = false) {
  if (deployPending || deployBusy(state.status?.status)) return;
  clickedTarget = config.variant; envError = null; deployError = ""; rollbackStatus = null; deployPending = true;
  renderDeploying(state.status || {});
  try {
    await api("/api/deploy", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    if (fromEnv) state.configDirty = false;
  } catch (err) {
    if (fromEnv) { clickedTarget = null; envError = err; } else deployError = err;
  }
  finally { await refreshStatus(); deployPending = false; renderDeploying(state.status || {}); }
}

let stopPending = false, stopError = null;
function canStop(status) {
  return !!status?.engine?.running && ["ready", "error"].includes(status.status);
}
function renderStop() {
  const button = $("btn-stop");
  button.classList.toggle("hidden", !canStop(state.status));
  button.disabled = stopPending || !canStop(state.status);
  setText("stop-feedback", stopError ? t("status.stopFailed", {reason:stopError.message}) : "");
}
async function stopEngine() {
  if (stopPending || !canStop(state.status)) return;
  stopPending = true; renderStop();
  try {
    await api("/api/stop", { method: "POST" });
    stopError = null;
  } catch (err) {
    stopError = err instanceof LocalizedError ? err : err.status
      ? new LocalizedError("api.error", {status:err.status}) : new LocalizedError("status.stopConnection");
  } finally {
    await refreshStatus(); stopPending = false; renderStop();
  }
  await loadHeads();
}

/* ---------------------------------------------------------------- глоссарий */

/* Фраза здесь — не тег, а слово, которому учат движок, поэтому чип несёт её судьбу:
   ту, что словарь развёрнутой головы написать не может, движок молча выбросит, и она
   показана зачёркнутой. Отдельного списка отброшенных больше нет — он и есть список. */

const glossary = { loaded:false, saving:false, request:0, payload:null, entries: [], applied: "", dropped: new Set(), envMissing: [], issues: [] };

function setGlossaryHint(render) {
  glossary.hint = render;
  setText("glossary-hint", render ? render() : "");
}

const fold = (phrase) => phrase.toLowerCase().replace(/ё/g, "е");

async function loadGlossary(keepEdits = true) {
  const request = ++glossary.request;
  try {
    const payload = await api("/api/glossary");
    if (request !== glossary.request) return false;
    const dirty = glossary.loaded && glossaryText() !== glossary.applied;
    const entries = parseGlossary(payload.text);
    if (!dirty || !keepEdits) glossary.entries = entries;
    glossary.applied = entryText(entries);
    glossary.payload = payload; glossary.loaded = true;
    glossary.dropped = new Set((payload.dropped || []).map(fold));
    glossary.issues = payload.issues || [];
    $("glossary-retry").hidden = true;
    renderGlossary(payload); renderSettingHints();
    return true;
  } catch (err) {
    if (request === glossary.request) {
      setGlossaryHint(() => err.message);
      $("glossary-retry").hidden = false;
    }
    return false;
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

const glossaryText = () => entryText(glossary.entries);
function entryText(entries) {
  return entries
    .map((entry) => (entry.weight ? entry.phrase + "|" + entry.weight : entry.phrase))
    .join("\n");
}

function renderGlossary(payload) {
  const list = $("tag-list");
  list.textContent = "";
  glossary.entries.forEach((entry, index) => list.appendChild(tagChip(entry, index)));

  $("glossary-count").textContent = phraseCount(glossary.entries.length);
  $("glossary-editor").disabled = !glossary.loaded || glossary.saving;
  $("btn-glossary").disabled = !glossary.loaded || glossary.saving || glossaryText() === glossary.applied;
  renderReach(payload || glossary.payload);
  renderIssues();
  renderEnvMissing(payload);
}

/* Чего человек не просил, но получит. Синтаксис `Фраза|8` выглядит как сила
   подсказки, а движок читает вес как выключатель: ноль и меньше выбрасывает
   фразу, любое положительное значение равно единице. Ещё строка с `#` уходит у
   него в комментарий. Ничего из этого из файла не видно, поэтому сервер отдаёт
   коды, а здесь они превращаются в фразы на языке интерфейса — по строке на
   причину, со списком затронутых фраз, чтобы шесть предупреждений об одном и том
   же не выстроились в столбик.

   Список описывает применённый глоссарий, а не то, что сейчас набрано в поле:
   он приходит с сервера и обновляется после «Применить». Так и честнее — до
   применения движок ничего из этого ещё не сделал. */

function renderIssues() {
  const box = $("glossary-issues");
  box.textContent = "";
  const byCode = new Map();
  for (const issue of glossary.issues || []) {
    if (!byCode.has(issue.code)) byCode.set(issue.code, []);
    byCode.get(issue.code).push(issue.phrase);
  }
  box.classList.toggle("hidden", byCode.size === 0);
  for (const [code, phrases] of byCode) {
    const line = document.createElement("p");
    line.className = "detail";
    line.textContent = t("issue." + code, { list: phrases.join(", ") });
    box.appendChild(line);
  }
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
  $("glossary-env-text").textContent = t("glossary.envMissing", {
    count: missing.length,
    list: missing.join(", "),
  });
  $("btn-glossary-env").onclick = () => addPhrases(missing.join("\n"));
}

function tagChip(entry, index) {
  const chip = document.createElement("li");
  chip.className = "tag";
  chip.dataset.index = String(index);
  if (glossary.dropped.has(fold(entry.phrase))) {
    chip.dataset.dropped = "true";
    chip.title = t("glossary.dropTitle");
  }

  const label = document.createElement("span");
  label.className = "tag-text";
  label.textContent = entry.phrase;
  chip.appendChild(label);

  if (entry.weight) {
    const weight = document.createElement("span");
    weight.className = "tag-weight";
    weight.textContent = "·" + entry.weight;
    weight.title = t("glossary.weight");
    chip.appendChild(weight);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "tag-remove";
  remove.textContent = "×";
  remove.setAttribute("aria-label", t("glossary.remove", { phrase: entry.phrase }));
  remove.addEventListener("click", () => {
    removePhrase(index);
    ($("tag-list").children[Math.min(index,glossary.entries.length-1)]?.querySelector("button") || $("add-phrase")).focus();
  });
  chip.appendChild(remove);
  return chip;
}

function renderReach(payload) {
  const reach = $("glossary-reach");
  const hint = $("dropped-hint");
  const total = glossary.entries.length;
  const dropped = glossary.dropped.size;
  const head = payload && payload.variant
    ? t("glossary.headNamed", { variant: payload.variant })
    : t("glossary.headAny");

  if (glossaryText() !== glossary.applied) {
    reach.textContent = t("glossary.staged"); hint.textContent = ""; return;
  }
  if (!total) {
    reach.textContent = "";
    hint.textContent = "";
    return;
  }
  if (payload && payload.usable_count === null) {
    reach.dataset.loss = "false";
    reach.textContent = t("glossary.notDownloaded");
    hint.textContent = "";
    return;
  }
  reach.dataset.loss = String(dropped > 0);
  reach.textContent = dropped
    ? t("glossary.lost", { dropped, total })
    : t("glossary.allReach") +
      (payload && payload.approximate ? t("glossary.approximate") : "");
  hint.textContent = dropped ? droppedHint(payload && payload.alphabet, head) : "";
}

/* Подсказка собирается из того, что в словаре на диске, а не из общего правила:
   для e2e_rnnt заглавные и латиница есть, для rnnt нет ни того, ни другого.
   Про регистр советовать нечего — движок сам пробует фразу строчными и с «е»
   вместо «ё», поэтому уцелеть тут может только другой алфавит. */
function droppedHint(alphabet, head) {
  if (!alphabet) return t("glossary.missingSymbols", { head });

  const missing = [];
  if (!alphabet.latin) missing.push(t("glossary.latin"));
  if (!alphabet.digits) missing.push(t("glossary.digits"));
  if (!missing.length) return t("glossary.missingSymbols", { head });
  const list = missing.join(t("glossary.listSeparator"));
  const advice = alphabet.latin ? "" : t("glossary.latinAdvice");
  return t("glossary.missingList", { head, missing: list }) + advice;
}

/* --- правка списка --- */

function mergedPhrases(raw) {
  const entries = [...glossary.entries], indices = new Map();
  entries.forEach((entry, index) => { const key = fold(entry.phrase); if (!indices.has(key)) indices.set(key, index); });
  const incoming = parseGlossary(raw); let duplicate = null, added = 0;
  for (const entry of incoming) {
    const key = fold(entry.phrase);
    if (indices.has(key)) { duplicate = indices.get(key); continue; }
    indices.set(key, entries.length); entries.push(entry); ++added;
  }
  return {entries, added, duplicate, skipped:incoming.length - added};
}
function addPhrases(raw) {
  const {entries, added, duplicate} = mergedPhrases(raw);
  glossary.entries = entries; renderGlossary(null);
  if (added) {
    setGlossaryHint(() => t("glossary.added"));
  } else if (duplicate !== null) {
    setGlossaryHint(() => t("glossary.duplicate"));
    flashChip(duplicate);
  }
  return added;
}

function flashChip(index) {
  const chip = $("tag-list").querySelector('[data-index="' + index + '"]');
  if (!chip) return;
  chip.scrollIntoView({ block: "nearest", behavior: "instant" });
  chip.dataset.flash = "true";
  setTimeout(() => delete chip.dataset.flash, 900);
}

function removePhrase(index) {
  glossary.entries.splice(index, 1);
  renderGlossary(null);
  setGlossaryHint(() => t("glossary.removed"));
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
function checkedGlossaryBody(text) {
  const body = JSON.stringify({text});
  if (state.mode === "owner" && new TextEncoder().encode(body).byteLength > state.jsonLimit) {
    setGlossaryHint(() => t("glossary.jsonLimit", {limit:state.jsonLimit.toLocaleString(currentLang())}));
    return null;
  }
  return body;
}
async function sendGlossary(text) {
  if (!glossary.loaded || glossary.saving) return;
  const body = checkedGlossaryBody(text);
  if (body === null) return;
  glossary.saving = true; ++glossary.request; renderGlossary(glossary.payload);
  setGlossaryHint(() => t("glossary.applying"));
  try {
    const payload = await api("/api/glossary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    const key = !payload.applied ? "glossary.applyFailed" : state.status?.engine?.running ? "glossary.applied" : "glossary.stored";
    setGlossaryHint(() => t(key));
    await loadGlossary(false);
    // Ответ на правку знает о ней больше, чем последующее чтение файла: файл не
    // хранит ни неразобранный вес, ни табуляцию. Поэтому список претензий из
    // POST сильнее того, что вернул GET, и перекрывает его.
    if (payload.issues) {
      glossary.issues = payload.issues;
      renderIssues();
    }
    return payload.count;
  } catch (err) {
    setGlossaryHint(() => err.message);
    return null;
  } finally { glossary.saving = false; renderGlossary(glossary.payload); }
}

function applyGlossary() {
  return sendGlossary(glossaryText());
}

/* Импорт дописывает к тому, что есть: повторы отсеются, а стереть собранный руками
   список одним неверным файлом было бы обидно. */
async function importGlossary(file) {
  const {entries, added, skipped} = mergedPhrases(await file.text());
  if (state.mode === "owner" && checkedGlossaryBody(entryText(entries)) === null) return 0;
  glossary.entries = entries; renderGlossary(null);
  setGlossaryHint(() => added
    ? t("glossary.imported", {
        added,
        skipped: skipped ? t("glossary.importedSkipped", { count: skipped }) : "",
      })
    : t("glossary.importedNothing"));
  return added;
}

function exportGlossary() {
  if (!glossary.entries.length) {
    setGlossaryHint(() => t("glossary.empty"));
    return;
  }
  const url = URL.createObjectURL(
    new Blob([glossaryText() + "\n"], { type: "text/plain;charset=utf-8" })
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = t("glossary.exportName");
  link.click();
  URL.revokeObjectURL(url);
  setGlossaryHint(() => t("glossary.saved"));
}

/* ----------------------------------------------------------------- проверка */

const audio = {mode:"mic", file:null, pending:false, starting:false, generation:0, micError:"", result:null, message:"audio.empty", error:""};
const motion = matchMedia("(prefers-reduced-motion: reduce)");
const micAvailable = () => window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
function renderAudio() {
  const mic = audio.mode === "mic", rec = !!state.recording, ready = state.status?.status === "ready";
  const busy = audio.starting || audio.pending;
  $("mic-mode").setAttribute("aria-pressed", mic); $("file-mode").setAttribute("aria-pressed", !mic);
  $("file-mode").disabled = rec || audio.starting;
  $("btn-record").hidden = !mic; $("file-actions").hidden = mic;
  $("btn-record").disabled = busy || (!rec && (!ready || !micAvailable()));
  $("btn-record").textContent = t(rec ? "btn.recordStop" : "audio.record");
  $("btn-send").disabled = !audio.file || !ready || busy;
  $("file-input").disabled = rec || audio.starting;
  setText("source-title", t(mic ? (rec ? "audio.recording" : "audio.mic") : "audio.file"));
  const sourceState = mic ? audio.micError?.message || (rec ? t("audio.live") : audio.starting ? t("audio.permission") : !micAvailable() ? t("mic.blockedTitle") : null) : audio.file?.name;
  const serviceState = !state.status ? state.statusError?.message || t("audio.awaitStatus") : !ready ? t("audio.notReady", {state:stateWord(state.status.status)}) : "";
  setText("source-state", [sourceState || (!serviceState ? t(mic ? "audio.idle" : "audio.pick") : ""), serviceState].filter(Boolean).join(" · "));
  $("drop").querySelector(".face").dataset.running = rec;
  $("meter").hidden = !rec || motion.matches; $("idlepoint").hidden = rec && !motion.matches;
  const result = audio.result;
  setText("result-text", result ? result.text || t("test.empty") : audio.error?.message || t(audio.message));
  $("result-text").classList.toggle("empty", !result?.text);
  $("result").hidden = !result;
  document.querySelector('[data-target="result-text"]').hidden = !result?.text;
  renderLatency(result);
}
function resetAudioResult() {
  audio.controller?.abort(); audio.controller = null; audio.pending = false;
  audio.result = null; audio.message = "audio.empty"; audio.error = "";
  document.querySelector("[data-page=dict] .manual")?.remove();
}
async function sendAudio(blob, filename, source = "file") {
  if (audio.pending || !blob || state.status?.status !== "ready") return;
  resetAudioResult();
  const controller = new AbortController(); audio.controller = controller;
  audio.pending = true; audio.message = "test.recognizing"; renderAudio();
  const form = new FormData(); form.append("file", blob, filename); form.append("source", source);
  try {
    const result = await api("/api/test", {method:"POST", body:form, signal:controller.signal});
    if (audio.controller === controller) audio.result = result;
  } catch (err) { if (audio.controller === controller) audio.error = err; }
  finally {
    if (audio.controller === controller) { audio.pending = false; audio.controller = null; renderAudio(); refreshStatus(); }
  }
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
  $("val-audio").textContent = audio
    ? t("unit.seconds", { value: audio.toFixed(1) })
    : t("latency.unknown");
  $("val-work").textContent = t("unit.seconds", { value: work.toFixed(2) });
  if (!audio) {
    verdict.textContent = t("latency.durationOnly");
    verdict.dataset.slow = "false";
    return;
  }
  if (!work) { verdict.textContent = ""; return; }
  const ratio = audio / work;
  verdict.dataset.slow = String(ratio < 1);
  verdict.textContent = ratio >= 1
    ? t("latency.faster", { ratio: ratio.toFixed(1) })
    : t("latency.slower", { ratio: (1 / ratio).toFixed(1) });
}

/* --------------------------------------------------- запись с микрофона → WAV */

const TARGET_RATE = 16000;

async function startRecording(generation) {
  const stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}});
  let ctx;
  try {
    if (generation !== audio.generation) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    ctx = new Ctx({sampleRate:TARGET_RATE});
    const source = ctx.createMediaStreamSource(stream), chunks = [], levels = [];
    let frame = 0;
    const onChunk = data => {
      if (generation !== audio.generation) return;
      chunks.push(data);
      if (motion.matches) return;
      let peak = 0;
      for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
      levels.push(peak); if (levels.length > 120) levels.shift();
      if (!frame) frame = requestAnimationFrame(() => {frame = 0; if (generation === audio.generation) drawMeter(levels);});
    };
    let node;
    if (ctx.audioWorklet) {
      await ctx.audioWorklet.addModule("/static/capture-worklet.js");
      node = new AudioWorkletNode(ctx, "capture");
      node.port.onmessage = e => onChunk(e.data);
    } else {
      node = ctx.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = e => onChunk(new Float32Array(e.inputBuffer.getChannelData(0)));
    }
    await ctx.resume();
    if (generation !== audio.generation) return;
    source.connect(node); node.connect(ctx.destination);
    state.recording = {stream, ctx, node, source, chunks};
  } finally {
    if (state.recording?.stream !== stream) {
      stream.getTracks().forEach(track => track.stop());
      if (ctx && ctx.state !== "closed") await ctx.close();
    }
  }
}

async function stopRecording(submit = true) {
  ++audio.generation;
  const rec = state.recording; state.recording = null; audio.starting = false;
  if (!rec) return;
  rec.source.disconnect(); rec.node.disconnect(); rec.node.port?.close();
  rec.stream.getTracks().forEach(track => track.stop());
  const rate = rec.ctx.sampleRate;
  const closing = rec.ctx.close();
  drawMeter([]); renderAudio();
  if (submit) {
    const samples = concat(rec.chunks);
    if (samples.length < rate * 0.25) {
      audio.result = null; audio.error = ""; audio.message = "test.tooShort"; renderAudio();
    } else if (state.status?.status === "ready") {
      sendAudio(encodeWav(resample(samples, rate, TARGET_RATE), TARGET_RATE), "microphone.wav", "microphone");
    } else { audio.result = null; audio.error = ""; audio.message = "audio.notSent"; renderAudio(); }
  }
  await closing;
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
  ctx.fillStyle = "#e5e7e4";
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
  if (state.recording) { await stopRecording(); return; }
  if (audio.starting) return;
  resetAudioResult(); audio.starting = true; audio.micError = "";
  const generation = ++audio.generation; renderAudio();
  try { await startRecording(generation); }
  catch (err) { if (audio.generation === generation) audio.micError = new LocalizedError("mic.error", {message:err.message}); }
  finally { if (audio.generation === generation) { audio.starting = false; renderAudio(); } }
}

/* --------------------------------------------------------------- сниппеты */

// Этот адрес попадает только в примеры и буфер обмена, но не в запросы консоли.
function clientBase(raw = $("base-url").value.trim()) {
  try {
    const url = new URL(raw);
    // URL переводит международные имена в ASCII; исходную запись проверяем отдельно,
    // чтобы не принять скрытые парсером /../, обратный слеш или пустые credentials.
    if (!/^https?:\/\/[^\s/@?#%\\]+(?:\/v1)?\/?$/i.test(raw) || url.username || url.password ||
        !/^[a-z\d.:[\]_-]+$/i.test(url.hostname) || (state.mode === "owner" && url.protocol !== "https:")) return null;
    return url.origin + "/v1";
  } catch { return null; }
}
function renderSnippets() {
  const base = clientBase(), auth = $("example-key").checked;
  const file = t("snippet.file"), sep = " \\\n  ";
  const bearer = auth ? [ '-H "Authorization: Bearer $STT_API_KEY"' ] : [];
  $("base-url").setAttribute("aria-invalid", String(!base));
  $("address-error").textContent = base ? "" : t(state.mode === "owner" ? "auth.addressInvalid" : "api.invalid");
  $("base-url").setCustomValidity($("address-error").textContent);
  $("snip-curl").textContent = base ? [`curl -X POST '${base}/audio/transcriptions'`, ...bearer, "-F 'model=kamerton'", `-F 'file=@${file}'`].join(sep) : "—";
  $("snip-models").textContent = base ? [`curl '${base}/models'`, ...bearer].join(sep) : "—";
  $("snip-py").textContent = base ? (auth ? "import os\n" : "") + "from openai import OpenAI\n\n" +
    `client = OpenAI(base_url="${base}", api_key=${auth ? 'os.environ["STT_API_KEY"]' : '"not-needed"'})\n` +
    `with open("${file}", "rb") as audio:\n    result = client.audio.transcriptions.create(model="kamerton", file=audio)\nprint(result.text)` : "—";
  document.querySelectorAll('[data-page="api"] .copy:not([data-target="client-model"])').forEach(b => b.disabled = !base);
  $("link-docs").href = state.mode === "local" && state.key ? "/api/docs?api_key=" + encodeURIComponent(state.key) : "/api/docs";
}
let modelsRequest = 0;
async function readServiceModels() {
  const request = ++modelsRequest, box = $("service-models");
  if (!box.open) { setText("model-response", ""); return; }
  setText("model-response", t("state.loading"));
  try {
    const result = await (state.mode === "owner" ? api("/api/client-models") : api("/v1/models"));
    if (request === modelsRequest && box.open) setText("model-response", JSON.stringify(result, null, 2));
  } catch (err) {
    if (request === modelsRequest && box.open) setText("model-response", err.message);
  }
}
let toastTimer;
function toast(text) {
  clearTimeout(toastTimer); $("live").textContent = text;
  toastTimer = setTimeout(() => { $("live").textContent = ""; }, 2800);
}
async function copyText(button) {
  const target = $(button.dataset.target), dialog = button.closest("dialog"), owner = page;
  const value = () => target.id === "base-url" ? clientBase() : target.textContent;
  const text = value(), current = () => owner === page && value() === text && (!dialog || dialog.open);
  if (!text || button.disabled) return;
  document.querySelector(".manual")?.remove();
  try {
    await navigator.clipboard.writeText(text);
    if (!current()) return;
    if (dialog?.id === "secret-dialog") setText("key-copy-status", t("btn.copied"));
    else toast(t("btn.copied"));
  } catch {
    if (!current() || !button.isConnected) return;
    const box = el("label", t("copy.manual"), "field manual"), area = el("textarea");
    area.readOnly = true; area.rows = 3; area.value = text; box.append(area);
    button.parentElement.after(box); area.focus(); area.select();
  }
}

/* ------------------------------------------------------------------ язык */

/* Абзацы, внутри которых есть код и ссылки, собираются здесь: класть разметку в
   словарь значило бы просить переводчика не сломать теги. Вместо этого в строке
   стоит подстановка, а чем её заполнить - решает код. */

function code(text) {
  const node = document.createElement("code");
  node.textContent = text;
  return node;
}

/* Строка с подстановками превращается в узлы: части между {name} - обычный текст,
   сами подстановки - готовые элементы. */
function rich(node, key, parts) {
  node.textContent = "";
  t(key, Object.keys(parts).reduce((acc, name) => {
    acc[name] = "\u0000" + name + "\u0000";
    return acc;
  }, {})).split("\u0000").forEach((chunk) => {
    const part = parts[chunk];
    if (part) node.appendChild(part);
    else if (chunk) node.appendChild(document.createTextNode(chunk));
  });
}

function renderRichTexts() {
  rich($("models-intro"), "models.intro", { volume: code("./models") });
  rich($("glossary-intro"), "glossary.intro", {});
  rich($("connect-intro"), "connect.intro", { prompt: code("prompt") });

}

const pageNames = Array.from(document.querySelectorAll("[data-page]"), n => n.dataset.page);
let page = null;
function showPage(focus = false) {
  const requested = location.hash.slice(1), next = pageNames.includes(requested) ? requested : "dict";
  if (page === "dict" && next !== page && (state.recording || audio.starting)) stopRecording(false);
  if (page === "compare" && next !== page) cancelComparison();
  if (next !== page) { document.querySelector(".manual")?.remove(); if (state.mode === "owner") ownerAccess.changeSection(); }
  const changed = next !== page; page = next;
  for (const node of document.querySelectorAll("[data-page]")) node.hidden = node.dataset.page !== page;
  for (const link of document.querySelectorAll("#railnav a")) {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  $("mobile-nav").value = page; $("heading").textContent = t("page." + page); $("intro").textContent = t("intro." + page);
  document.title = t("page." + page) + " — " + t("brand.name");
  if (focus) { $("main").focus({preventScroll:true}); window.scrollTo(0,0); }
  if (changed && page === "api" && state.mode === "owner") ownerAccess.load();
  if (changed && page === "journal") loadJournal();
  if (changed && (page === "models" || page === "compare")) loadHeads();
  if (changed && page === "glossary" && !glossary.saving) loadGlossary();
}
function options(id, entries) {
  const select = $(id), previous = select.value;
  select.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
  if (entries.some(([value]) => value === previous)) select.value = previous;
}
function localizedOptions() {
  options("mobile-nav", pageNames.map(k => [k,t("page." + k)]));
  for (const id of ["opt-punctuation","opt-itn"]) options(id,["auto","on","off"].map(v=>[v,t("mode."+v)]));
  options("opt-vad", [["false",t("vad.off")],["true",t("vad.on")]]);
  options("opt-hotwords-default", [["true",t("dict.on")],["false",t("dict.off")]]);
  for (const [id,key] of [["opt-pool","pool"],["opt-window-concurrency","window"]]) {
    const vals = Array.from({length:8},(_,i)=>String(i+1));
    options(id, vals.map(v => { const k = `${key}.${v}`; return [v,t(k) === k ? v : t(k)]; }));
  }
  for (const [id, keys] of [["category",["deploy","engine"]],["level",["info","warning","error"]],["period",["hour","day"]]]) {
    options("journal-" + id, ["all",...keys].map(v=>[v,t("journal."+v)]));
  }
}
function localize() {
  applyStaticTexts(); localizedOptions(); renderRichTexts(); renderSnippets(); markLangButtons();
  if (state.mode === "owner") ownerAccess.render();
  showPage(); renderAudio(); renderGlossary(glossary.payload); renderJournal(); renderCompare(true);
  if (state.status) renderStatus(state.status);
  else if (state.statusError) { renderStop(); setText("state-detail", state.statusError.message); setText("state-label", t("state.offline")); }
}
function switchLang(next) {
  if (!setLang(next)) return;
  document.querySelector(".manual")?.remove();
  localize(); setGlossaryHint(glossary.hint); loadHeads();
}

function markLangButtons() {
  document.querySelectorAll(".lang-option").forEach((button) => {
    const on = button.dataset.lang === currentLang();
    button.setAttribute("aria-pressed", String(on));
    button.classList.toggle("on", on);
  });
}

/* ------------------------------------------------------------------- логи */

function appendLog(line) {
  const logs = $("logs");
  logs.textContent += (logs.textContent ? "\n" : "") + line;
  const lines = logs.textContent.split("\n");
  if (lines.length > 400) logs.textContent = lines.slice(-400).join("\n");
  if ($("log-follow").checked) logs.scrollTop = logs.scrollHeight;
}

let eventSource, reconnectTimer;
function connectEvents() {
  clearTimeout(reconnectTimer); eventSource?.close();
  if (state.leaving) return;
  const query = state.mode === "local" && state.key ? "?api_key=" + encodeURIComponent(state.key) : "";
  const source = new EventSource("/api/events" + query);
  eventSource = source;
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
      $("logs").textContent = "";
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
    reconnectTimer = setTimeout(connectEvents, 3000);
  };
}

/* --------------------------------------------- структурированный журнал */
const journal = {events:null, error:"", loading:false};
async function loadJournal() {
  if (journal.loading) return;
  journal.loading = true;
  try {
    const payload = await api("/api/journal");
    journal.events = payload.events; journal.error = "";
  } catch (err) { journal.error = err; }
  finally { journal.loading = false; renderJournal(); }
}
function renderJournal() {
  const kind = $("journal-category").value, level = $("journal-level").value, period = $("journal-period").value;
  const after = period === "hour" ? Date.now() - 3600000 : period === "day" ? Date.now() - 86400000 : 0;
  const events = (journal.events || []).filter(e => (kind === "all" || kind === e.category) &&
    (level === "all" || level === e.level) && Date.parse(e.at) >= after);
  $("journal-note").textContent = journal.error?.message || (!journal.events ? t("state.loading") : !events.length ? t("journal.empty") : "");
  const fmt = new Intl.DateTimeFormat(currentLang(),{dateStyle:"short", timeStyle:"medium"});
  const rows = events.map(event => {
    const li = el("li", null, "logrow"), time = el("time", fmt.format(new Date(event.at))), body = el("div");
    time.dateTime = event.at; li.dataset.level = event.level;
    const p = event.params || {}, key = "event." + event.code;
    const message = t(key, {...p, kind:t("reason."+(p.kind || "other")), variant:p.variant || "—"});
    body.append(el("span",t("journal."+event.category)+" · "+t("journal."+event.level),"logmeta"), el("span",message === key ? event.code : message));
    if (event.code === "download.failed" && p.running) body.append(el("span",t("event.previous",p),"logmeta"));
    li.append(time,body); return li;
  });
  $("journal-events").replaceChildren(...rows);
}

/* ------------------------------------------------------- сравнение моделей */
const compare = {saving:false, saveError:null, busy:false, signature:null, payload:null, error:""};
function renderCompare(force = false) {
  const downloaded = [...state.heads.values()].map(item => item.head).filter(head => head.downloaded);
  const enabled = state.status?.compare_enabled === true, changing = deployBusy(state.status?.status);
  const protectedMode = state.mode === "owner" || !!state.status?.api_key_set;
  const canToggle = state.status?.compare_available ?? protectedMode;
  const toggle = $("compare-enabled");
  if (!compare.saving) toggle.checked = enabled;
  toggle.disabled = !state.status || (!canToggle && !enabled) || compare.saving;
  setText("compare-toggle-note", t(!state.status || canToggle ? "compare.toggleNote" : protectedMode ? "compare.noTemp" : "compare.needsKey"));
  setText("compare-toggle-error", compare.saveError?.message || "");
  const available = enabled && !compare.saving && state.catalog === "ready" && downloaded.length >= 2 && !changing;
  const message = !state.status ? "audio.awaitStatus" : !enabled ? "compare.disabled" : changing ? "compare.engineChanging"
    : state.catalog === "loading" ? "compare.catalogLoading" : state.catalog === "error" ? "compare.catalogError"
    : downloaded.length < 2 ? "compare.weights" : "compare.method";
  setText("compare-availability", !state.status && state.statusError?.message || t(message));
  $("compare-form").hidden = !available; $("compare-models").hidden = !["compare.catalogError","compare.weights"].includes(message);
  const signature = downloaded.map(h=>h.id).join("|");
  if (signature !== compare.signature) {
    compare.signature = signature;
    const entries = downloaded.map(h => [h.id,h.title]);
    for (const id of ["model-a","model-b"]) options(id, entries);
    if ($("model-a").value === $("model-b").value && downloaded.length > 1) {
      $("model-b").value = downloaded.find(h => h.id !== $("model-a").value).id;
    }
  }
  for (const id of ["model-a","model-b"]) { $(id).disabled = compare.busy; $(id).dataset.previous = $(id).value; }
  const file = $("compare-file").files[0];
  $("compare-name").textContent = file?.name || t("compare.noFile");
  $("compare-pick-label").textContent = t(file ? "compare.change" : "btn.file");
  $("compare-picker").classList.toggle("primary", !file);
  $("btn-compare").hidden = !file;
  $("compare-file").disabled = compare.busy; $("btn-compare").disabled = !available || compare.busy || !file;
  if (!compare.payload) $("compare-stage").textContent = compare.busy ? t("compare.wait") : compare.error?.message || "";
  if (force) showComparison();
}

/* Совпадающие слова, знаки и пробелы выравниваются по наибольшей общей подпоследовательности.
   Для очень длинных расшифровок остаётся точный ответ о равенстве, без тяжёлой подсветки. */
function diffTokens(textA, textB) {
  const tokens = s => s.match(/[\p{L}\p{M}\p{N}]+|\s+|[^\s\p{L}\p{M}\p{N}]/gu) || [];
  const a = tokens(textA), b = tokens(textB), n = a.length, m = b.length;
  let start = 0, end = 0;
  while (start < n && start < m && a[start] === b[start]) start++;
  while (end < n-start && end < m-start && a[n-1-end] === b[m-1-end]) end++;
  const rows = n-start-end, cols = m-start-end;
  if ((rows+1)*(cols+1) > 1000000) return null;
  const sameA = new Uint8Array(n), sameB = new Uint8Array(m);
  for (const flags of [sameA,sameB]) {flags.fill(1,0,start); flags.fill(1,flags.length-end);}
  const stride = cols+1, grid = new Uint32Array((rows+1)*stride);
  for (let i=rows-1;i>=0;i--) for (let j=cols-1;j>=0;j--) {
    const p = i*stride+j;
    grid[p] = a[start+i] === b[start+j] ? 1+grid[p+stride+1] : Math.max(grid[p+stride],grid[p+1]);
  }
  let i=0,j=0;
  while (i<rows && j<cols) {
    if (a[start+i] === b[start+j]) {sameA[start+i++]=1; sameB[start+j++]=1;}
    else if (grid[(i+1)*stride+j] >= grid[i*stride+j+1]) i++; else j++;
  }
  return [[a,sameA],[b,sameB]];
}
function showComparison() {
  const box = $("compare-results"); box.replaceChildren();
  const payload = compare.payload;
  $("compare-time-note").hidden = !payload;
  if (!payload) return;
  const [a,b] = payload.results, same = a.text === b.text;
  const diff = same ? null : diffTokens(a.text,b.text);
  $("compare-stage").textContent = t(same ? "compare.same" : diff ? "compare.diff" : "compare.long") +
    (Number.isFinite(payload.audio_seconds) ? " · " + t("compare.audio", {value:payload.audio_seconds.toFixed(2)}) : "");
  payload.results.forEach((r,i) => {
    const card = el("section",null,"panel stack"), text = el("p",null,"sample"), btn = el("button",t("btn.copy"),"copy");
    text.id = "comparison-"+i; btn.type = "button"; btn.dataset.target = text.id; btn.disabled = !r.text;
    if (!diff || !r.text) text.textContent = r.text || t("test.empty");
    else {
      const [tokens,flags] = diff[i];
      for (let start=0;start<tokens.length;) {
        let end = start+1; while (end<tokens.length && flags[start] === flags[end]) end++;
        const value = tokens.slice(start,end).join(""); text.append(flags[start] ? document.createTextNode(value) : el("mark",value)); start=end;
      }
    }
    card.append(el("span",t(i ? "compare.second" : "compare.first"),"small"),el("h2",state.heads.get(r.variant)?.head.title || r.variant),
      code(r.variant), text, el("p",t("compare.elapsed",{value:r.elapsed.toFixed(2)}),"help"),btn); box.append(card);
  });
}
function cancelComparison() {
  if (!compare.controller) return;
  compare.controller.abort(); compare.controller = null; compare.busy = false;
  compare.error = ""; compare.payload = null; renderCompare(true);
}
async function runCompare(event) {
  event.preventDefault(); if ($("btn-compare").disabled) return;
  const file = $("compare-file").files[0], form = new FormData();
  form.append("file",file,file.name); form.append("variant_a",$("model-a").value); form.append("variant_b",$("model-b").value);
  const controller = new AbortController(); compare.controller = controller;
  compare.busy = true; compare.payload = null; compare.error = ""; renderCompare(true);
  try {
    const result = await api("/api/compare",{method:"POST",body:form,signal:controller.signal});
    if (compare.controller === controller) compare.payload = result;
  } catch (err) { if (compare.controller === controller) {compare.error = err; loadHeads();} }
  finally { if (compare.controller === controller) { compare.controller = null; compare.busy = false; renderCompare(true); } }
}

/* -------------------------------------------------------------------- init */

function bindAudio() {
  for (const mode of ["mic","file"]) $(mode+"-mode").onclick = () => {
    if (mode !== audio.mode) { resetAudioResult(); audio.mode = mode; renderAudio(); }
  };
  $("btn-record").onclick = toggleRecording; motion.addEventListener("change", renderAudio);
  const chooseFile = file => { if (!file || state.recording || audio.starting) return; resetAudioResult(); audio.file=file; audio.mode="file"; renderAudio(); };
  $("file-input").onchange = e => chooseFile(e.target.files[0]);
  $("btn-send").onclick = () => sendAudio(audio.file, audio.file?.name);
  const drop = $("drop");
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); chooseFile(e.dataTransfer.files[0]); };
  window.addEventListener("pagehide", () => { if (state.recording || audio.starting) stopRecording(false); cancelComparison(); });
}
function init() {
  $("service-models").ontoggle = readServiceModels;
  const base = $("base-url"), localBase = location.origin + "/v1";
  base.value = localBase;
  try { base.value = clientBase(localStorage.getItem("kamerton_client_base") || "") || localBase; } catch {
    /* Хранилище отключено. */
  }
  const updateExamples = () => { document.querySelector('[data-page="api"] .manual')?.remove(); renderSnippets(); };
  base.oninput = updateExamples;
  base.onchange = () => {
    const canonical = clientBase();
    if (canonical) {
      base.value = canonical;
      try { localStorage.setItem("kamerton_client_base", canonical); } catch {
        /* Только в этой вкладке. */
      }
    }
    updateExamples();
  };
  $("example-key").onchange = updateExamples;
  $("reset-origin").onclick = () => {
    base.value = localBase;
    try { localStorage.removeItem("kamerton_client_base"); } catch {
      /* Хранилище отключено. */
    }
    updateExamples(); base.focus();
  };
  $("mobile-nav").onchange = e => { location.hash = e.target.value; };
  document.querySelector(".skip").onclick = e => { e.preventDefault(); $("main").focus(); };
  window.addEventListener("hashchange", () => showPage(true));
  document.addEventListener("click", e => { const b = e.target.closest("button.copy"); if (b) copyText(b); });
  document.querySelectorAll(".lang-option").forEach(b => b.onclick = () => switchLang(b.dataset.lang));
  $("heads").onchange = e => { state.selected = e.target.value; state.configDirty = true; renderSelection(); };
  $("config-form").oninput = () => { state.configDirty = true; renderSettingHints(); renderDeploying(state.status || {}); };
  $("config-form").onsubmit = e => { e.preventDefault(); if (!$("btn-deploy").disabled) deploy(draftConfig()); };
  $("legacy-access").hidden = state.mode !== "local";
  $("edit-key").disabled = state.mode !== "local";
  if (state.mode === "owner") ownerAccess.bind();
  else $("edit-key").onclick = async () => {
    const entered = window.prompt(t("api.keyPrompt"), "");
    if (entered === null) return;
    $("service-models").open = false;
    state.key = entered.trim(); state.keyAsked = true;
    try { localStorage.setItem(KEY_STORAGE,state.key); } catch { /* Только в этой вкладке. */ }
    renderSnippets(); await refreshStatus(); loadHeads(); loadGlossary(); connectEvents();
  };
  $("btn-deploy-env").onclick = deployFromEnv; $("btn-stop").onclick = stopEngine;
  $("btn-add").onclick = commitInput; $("btn-glossary").onclick = applyGlossary;
  $("glossary-retry").onclick = async () => { if (await loadGlossary()) setGlossaryHint(null); };
  $("btn-export").onclick = exportGlossary;
  $("glossary-file").onchange = async e => {
    const file=e.target.files[0]; e.target.value=""; if (!file) return;
    try { await importGlossary(file); } catch (err) { setGlossaryHint(() => err.message); }
  };
  $("add-phrase").onkeydown = e => {
    if (e.isComposing) return;
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commitInput(); }
    else if (e.key === "Backspace" && !e.target.value && glossary.entries.length) removePhrase(glossary.entries.length-1);
  };
  $("add-phrase").onpaste = e => {
    const text = e.clipboardData?.getData("text");
    if (text && /[\n,]/.test(text)) { e.preventDefault(); addPhrases(text); e.target.value=""; }
  };
  $("compare-enabled").onchange = async e => {
    const enabled = e.target.checked, alreadyRunning = compare.busy;
    compare.saving = true; compare.saveError = null; renderCompare();
    try {
      const saved = await api("/api/settings/compare", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled})});
      if (state.status) state.status.compare_enabled = saved.enabled;
      if (!saved.enabled && !alreadyRunning) { compare.payload = null; compare.error = ""; }
    } catch (err) { compare.saveError = err; await refreshStatus(); }
    finally { compare.saving = false; renderCompare(true); }
  };
  $("compare-form").onsubmit = runCompare;
  $("compare-form").onchange = e => {
    if (e.target.matches("select")) {
      const other = $(e.target.id === "model-a" ? "model-b" : "model-a");
      if (other.value === e.target.value) other.value = e.target.dataset.previous;
    }
    compare.error=""; compare.payload=null; renderCompare(true);
    if (e.target.id === "compare-file" && e.target.files.length) $("btn-compare").focus();
  };
  for (const id of ["journal-category","journal-level","journal-period"]) $(id).onchange = renderJournal;
  bindAudio(); localize();
  refreshStatus().then(() => { loadHeads(); loadGlossary(); connectEvents(); });
  setInterval(() => { refreshStatus(); if (page === "journal") loadJournal(); }, 5000);
}

async function checkSession() {
  const retry = $("session-retry");
  retry.hidden = true; setText("session-message", t("auth.checking"));
  try {
    // До явного ответа local даже не читаем прежний ключ.
    const session = await readSession();
    if (session.mode === "owner") {
      try { localStorage.removeItem(KEY_STORAGE); } catch { /* Хранилище отключено. */ }
      acceptOwnerSession(session);
    } else if (session.mode === "local") {
      try { state.key = localStorage.getItem(KEY_STORAGE) || ""; }
      catch { /* Хранилище отключено. */ }
    } else throw new Error();
    state.mode = session.mode;
  } catch {
    setText("session-message", t("auth.checkFailed")); retry.textContent = t("head.retry"); retry.hidden = false; return;
  }
  $("session-check").hidden = true; document.querySelector(".shell").hidden = false; init();
}
$("session-retry").onclick = checkSession;
checkSession();
