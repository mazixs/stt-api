/* Язык интерфейса: английский по умолчанию, русский - выбором пользователя.

   Переводы живут здесь целиком, включая строки, которые собирает app.js: держать
   половину фраз в разметке, а половину в коде значит рано или поздно перевести
   только половину. Ключ - это то, что фраза значит, а не то, как она звучит.

   Английский - запасной язык для любого пропущенного ключа: пустая подпись хуже
   подписи не на том языке. */

/* Файл подключается обычным <script>, поэтому всё живёт внутри функции: иначе
   `t` и `LANGS` оказались бы в общем пространстве имён вместе с app.js. Наружу
   выходит только `window.i18n`. */
(function () {
const LANGS = ["en", "ru"];
const DEFAULT_LANG = "en";
const LANG_STORAGE = "stt_console_lang";

const TEXT = {
  en: {
    "doc.title": "STT Console · GigaAM v3",
    "brand.eyebrow": "local speech recognition",
    "brand.suffix": "/ STT Console",
    "lang.label": "Interface language",

    "state.loading": "loading…",
    "state.stopped": "not deployed",
    "state.downloading": "downloading the model",
    "state.starting": "starting the engine",
    "state.ready": "ready",
    "state.error": "error",
    "key.badge": "key required",

    "section.status": "Status",
    "btn.stop": "Stop",
    "ro.variant": "model",
    "ro.elapsed": "latency, average",
    "ro.rtf": "faster than speech",
    "ro.total": "files, all time",
    "ro.restarts": "engine restarts",
    "ro.uptime": "console uptime",
    "recent.title": "recent files",
    "recent.file": "file",
    "recent.audio": "recording",
    "recent.work": "recognition",
    "recent.faster": "faster than speech",
    "recent.average": "average latency all time - {value} s per file",

    "section.models": "Models",
    "models.intro":
      "Pick a recognition head. The first run downloads the weights once - after that they stay in the {volume} volume.",
    "head.deploy": "Deploy",
    "head.redeploy": "Redeploy",
    "head.retry": "Try again",
    "head.downloaded": "downloaded",
    "head.nativePunctuation": "punctuation in the model",
    "head.size": "~{mb} MB",

    "phase.verifying": "checking the weights",
    "phase.downloading": "downloading {percent}%",
    "phase.starting": "starting the engine",
    "phase.building": "building the graph, up to 2 minutes on a first run",
    "phase.rollback": "rolling back to the previous head",
    "deploy.failed": "could not deploy",

    "options.summary": "Startup settings",
    "opt.punctuation": "Punctuation and casing",
    "opt.itn": "Numbers as digits",
    "opt.vad": "Silence skipping (VAD)",
    "opt.pool": "Concurrent recognitions",
    "opt.pool.note":
      "Go above 1 only together with parallel windows: the engine splits encoder threads between slots, and an idle second slot slows a single request down.",
    "opt.window": "File windows at once",
    "opt.window.note":
      "Only for the {rnnt} and {e2e} heads, where long recordings get about 30% faster. On {mlctc} and {mlctcLarge} it costs 5-12%, so leave it off. Requires \"concurrent recognitions\" of 2; the text does not change.",
    "opt.hotwordsDefault": "Built-in brand dictionary",
    "opt.boost": "Glossary hint strength",
    "mode.auto": "let the model decide",
    "mode.on": "on",
    "mode.off": "off",
    "vad.on": "on",
    "vad.off": "off",
    "pool.1": "1 - lowest latency",
    "window.1": "1 - one after another",
    "window.2": "2 - two windows in parallel, needs pool 2",
    "dict.on": "on - the engine's 30 consumer brands",
    "dict.off": "off",

    "env.title": ".env asks for something else",
    "env.deploy": "Deploy with .env settings",
    "env.line": "{field}: .env says {env}, deployed {state}",
    "env.value.on": "on",
    "env.value.off": "off",
    "field.variant": "head",
    "field.punctuation": "punctuation",
    "field.itn": "numbers as digits",
    "field.vad": "silence skipping",
    "field.pool_size": "concurrent recognitions",
    "field.hotwords_boost": "hint strength",
    "field.hotwords_default": "brand dictionary",
    "field.file_window_concurrency": "windows in parallel",

    "section.test": "Check",
    "test.intro":
      "Dictate a phrase or drop a file in - you will see the text and the time it took on this server.",
    "btn.record": "Record from microphone",
    "btn.recordStop": "Stop recording",
    "btn.file": "Choose a file",
    "drop.hint": "…or drop an audio file here",
    "drop.hintInsecure":
      "Drop an audio file here - microphone recording works only on localhost or over HTTPS",
    "mic.blockedTitle": "The browser allows recording only on localhost or over HTTPS",
    "mic.error":
      "Microphone unavailable: {message}. Browsers allow recording only on localhost or over HTTPS.",
    "test.recognizing": "recognizing…",
    "test.empty": "(empty - silence or too short a recording)",
    "test.tooShort": "The recording is too short - say a longer phrase.",
    "latency.audio": "recording",
    "latency.work": "recognition",
    "latency.unknown": "unknown",
    "latency.durationOnly": "duration is known only for WAV and WebM",
    "latency.faster": "{ratio}x faster than real time",
    "latency.slower": "{ratio}x slower than the recording",

    "section.glossary": "Glossary",
    "glossary.intro":
      "Names and terms the engine should get right. Enter adds a phrase, the hint weight goes after a vertical bar: {example}. The list is applied during recognition, not by editing finished text.",
    "glossary.placeholder": "Add a phrase…",
    "glossary.addAria": "Add a phrase to the glossary",
    "glossary.add": "Add",
    "glossary.remove": "Remove “{phrase}”",
    "glossary.weight": "hint weight",
    "glossary.none": "no phrases",
    "glossary.count": "{count, plural, one {# phrase} other {# phrases}}",
    "glossary.dropTitle": "the engine cannot write this and will drop the phrase",
    "glossary.notDownloaded":
      "the weights are not downloaded yet - what reaches the engine will be visible after deployment",
    "glossary.lost":
      "the engine will drop what is struck through: {dropped, plural, one {# phrase} other {# phrases}} of {total}",
    "glossary.allReach": "every phrase reaches the engine",
    "glossary.approximate": "; the estimate is approximate",
    "glossary.headNamed": "the {variant} head",
    "glossary.headAny": "the selected head",
    "glossary.missingSymbols": "Some of these characters are missing from {head}'s vocabulary.",
    "glossary.missingList": "{head}'s vocabulary has no {missing}.",
    "glossary.latinAdvice": " Write such names in Russian: опенвиспр instead of OpenWhispr.",
    "glossary.listSeparator": " or ",
    "glossary.example": "Brandname|8",
    "glossary.latin": "Latin script",
    "glossary.digits": "digits",
    "glossary.added": "added, press “Apply”",
    "glossary.duplicate": "already in the list",
    "glossary.removed": "removed, press “Apply”",
    "glossary.applying": "applying…",
    "glossary.applied": "the engine re-read the glossary",
    "glossary.applyFailed": "could not apply",
    "glossary.imported":
      "added from the file: {added} {phrases}{skipped} - press “Apply”",
    "glossary.importedSkipped": ", duplicates skipped: {count}",
    "glossary.importedNothing": "no new phrases in the file",
    "glossary.empty": "the glossary is empty",
    "glossary.saved": "file saved",
    "glossary.exportName": "glossary.txt",
    "glossary.envMissing":
      ".env (INITIAL_CONTEXT) has {count, plural, one {# phrase} other {# phrases}} missing from the list: {list}.",
    "glossary.envAdd": "Add from .env",
    "btn.apply": "Apply",
    "btn.import": "Import",
    "btn.export": "Export",

    "section.connect": "Connecting",
    "connect.intro":
      "The API is OpenAI-compatible. The engine accepts the {prompt} field but ignores it - context is set by the glossary above.",
    "connect.docs":
      "Control API schema - {link}. Model list - {models}; the {model} field may hold anything, the deployed head is the one that runs.",
    "connect.swagger": "Swagger at /api/docs",
    "snippet.python": "Python, openai SDK",
    "snippet.models": "model list",
    "btn.copy": "Copy",
    "btn.copied": "Copied",
    "snippet.file": "recording.wav",
    "snippet.noKey": "not-needed",

    "section.logs": "Logs",
    "logs.follow": "follow",
    "foot.engine": "engine",
    "foot.model": "model",

    "api.keyPrompt": "The service is protected by a key. Enter the API_KEY from your .env file:",
    "api.error": "Error {status}",

    "detail.deploy.downloading": "Checking and downloading the model ({variant})",
    "detail.deploy.starting": "Starting the engine ({variant})",
    "detail.deploy.ready": "Ready: {variant}",
    "detail.download.retry": "{reason} Trying again ({attempt} of {attempts})…",
    "detail.download.failed": "{reason}",
    "detail.download.failedAttempts": " Attempts made: {attempts}.",
    "detail.download.failedRunning": " The previous model ({running}) keeps running.",
    "detail.start.failed": "Could not start model {variant}: {reason}",
    "detail.rollback.starting": "Rolling back to the previous model ({variant})",
    "detail.rollback.done":
      "Rolled back to the previous model ({variant}): model {failed} did not start - {reason}",
    "detail.rollback.failed":
      "Model {failed} did not start ({reason}), and the rollback to {variant} failed too.",
    "detail.glossary.restarting": "Restarting the engine for the new glossary",
    "detail.glossary.restart_failed": "Could not restart the engine after the glossary edit",
    "detail.idle.autostart_off": "Autostart is off (AUTOSTART=0)",
    "detail.idle.no_model": "No model selected - press “Deploy”",
    "detail.idle.stopped": "The engine is stopped",
    "detail.watchdog.exited": "The engine exited unexpectedly (code {code}), bringing it back up",
    "detail.watchdog.restarting": "Bringing the engine back up ({variant})",
    "detail.watchdog.ready": "Ready: {variant} (after a restart)",
    "detail.watchdog.failed": "The engine did not come up (attempt {attempt}), trying again",
    "download.checksum":
      "The checksum of the downloaded file did not match - the file is corrupted. Press “Deploy” again and the broken file will be fetched anew.",
    "download.network":
      "Network error while downloading the model. Check the server's internet access and try again.",
    "download.disk":
      "Could not write the model to disk. Check free space and the permissions on ./models.",
    "download.interrupted": "The model download was interrupted.",
    "download.other": "The model download failed.",

    "time.hours": "{hours} h {minutes} min",
    "time.minutes": "{minutes} min",
    "time.seconds": "{seconds} s",
    "unit.seconds": "{value} s",
  },

  ru: {
    "doc.title": "STT-консоль · GigaAM v3",
    "brand.eyebrow": "локальное распознавание речи",
    "brand.suffix": "/ STT-консоль",
    "lang.label": "Язык интерфейса",

    "state.loading": "загрузка…",
    "state.stopped": "не развёрнуто",
    "state.downloading": "скачиваю модель",
    "state.starting": "запускаю движок",
    "state.ready": "готово",
    "state.error": "ошибка",
    "key.badge": "ключ включён",

    "section.status": "Статус",
    "btn.stop": "Остановить",
    "ro.variant": "модель",
    "ro.elapsed": "задержка, среднее",
    "ro.rtf": "быстрее речи",
    "ro.total": "файлов за всё время",
    "ro.restarts": "перезапусков движка",
    "ro.uptime": "консоль работает",
    "recent.title": "последние файлы",
    "recent.file": "файл",
    "recent.audio": "запись",
    "recent.work": "распознавание",
    "recent.faster": "быстрее речи",
    "recent.average": "средняя задержка за всё время — {value} с на файл",

    "section.models": "Модели",
    "models.intro":
      "Выберите голову распознавания. Первый запуск скачивает веса один раз — дальше они лежат в томе {volume}.",
    "head.deploy": "Развернуть",
    "head.redeploy": "Перезапустить",
    "head.retry": "Повторить",
    "head.downloaded": "скачано",
    "head.nativePunctuation": "пунктуация в модели",
    "head.size": "~{mb} МБ",

    "phase.verifying": "проверяю веса",
    "phase.downloading": "скачиваю {percent}%",
    "phase.starting": "запускаю движок",
    "phase.building": "собираю граф, первый запуск до 2 минут",
    "phase.rollback": "откат на прежнюю голову",
    "deploy.failed": "не удалось развернуть",

    "options.summary": "Настройки запуска",
    "opt.punctuation": "Пунктуация и регистр",
    "opt.itn": "Числа цифрами",
    "opt.vad": "Пропуск тишины (VAD)",
    "opt.pool": "Одновременных распознаваний",
    "opt.pool.note":
      "Больше 1 берите только вместе с параллельными окнами: движок делит потоки энкодера между слотами, и пустой второй слот замедляет одиночный запрос.",
    "opt.window": "Окон файла одновременно",
    "opt.window.note":
      "Только для голов {rnnt} и {e2e}: там длинные записи быстрее примерно на 30%. На {mlctc} и {mlctcLarge} замедляет на 5-12%, включать не стоит. Требует \"одновременных распознаваний\" 2, текст не меняет.",
    "opt.hotwordsDefault": "Встроенный словарь брендов",
    "opt.boost": "Сила подсказки глоссария",
    "mode.auto": "как решит модель",
    "mode.on": "включить",
    "mode.off": "выключить",
    "vad.on": "включён",
    "vad.off": "выключен",
    "pool.1": "1 — минимальная задержка",
    "window.1": "1 - последовательно",
    "window.2": "2 - два окна параллельно, нужен пул 2",
    "dict.on": "включен - 30 бытовых брендов движка",
    "dict.off": "выключен",

    "env.title": "в .env указано другое",
    "env.deploy": "Развернуть с настройками .env",
    "env.line": "{field}: в .env {env}, развёрнуто {state}",
    "env.value.on": "включен",
    "env.value.off": "выключен",
    "field.variant": "голова",
    "field.punctuation": "пунктуация",
    "field.itn": "числа цифрами",
    "field.vad": "пропуск тишины",
    "field.pool_size": "одновременных распознаваний",
    "field.hotwords_boost": "сила подсказки",
    "field.hotwords_default": "словарь брендов",
    "field.file_window_concurrency": "окон параллельно",

    "section.test": "Проверка",
    "test.intro":
      "Надиктуйте фразу или перетащите файл — увидите текст и время на этом сервере.",
    "btn.record": "Записать с микрофона",
    "btn.recordStop": "Остановить запись",
    "btn.file": "Выбрать файл",
    "drop.hint": "…или перетащите сюда аудиофайл",
    "drop.hintInsecure":
      "Перетащите сюда аудиофайл — запись с микрофона доступна только на localhost или по HTTPS",
    "mic.blockedTitle": "Браузер разрешает запись только на localhost или по HTTPS",
    "mic.error":
      "Микрофон недоступен: {message}. Браузеры разрешают запись только на localhost или по HTTPS.",
    "test.recognizing": "распознаю…",
    "test.empty": "(пусто — тишина или слишком короткая запись)",
    "test.tooShort": "Запись слишком короткая — скажите фразу подольше.",
    "latency.audio": "запись",
    "latency.work": "распознавание",
    "latency.unknown": "неизвестно",
    "latency.durationOnly": "длительность известна только для WAV и WebM",
    "latency.faster": "быстрее реального времени в {ratio} раза",
    "latency.slower": "медленнее записи в {ratio} раза",

    "section.glossary": "Глоссарий",
    "glossary.intro":
      "Имена, термины и названия, которые движок должен узнавать. Фразу добавляет Enter, вес подсказки — после вертикальной черты: {example}. Список применяется на этапе распознавания, а не правкой готового текста.",
    "glossary.placeholder": "Добавить фразу…",
    "glossary.addAria": "Добавить фразу в глоссарий",
    "glossary.add": "Добавить",
    "glossary.remove": "Убрать «{phrase}»",
    "glossary.weight": "вес подсказки",
    "glossary.none": "фраз нет",
    "glossary.count": "{count, plural, one {# фраза} few {# фразы} other {# фраз}}",
    "glossary.dropTitle": "движок не сможет это написать и выбросит фразу",
    "glossary.notDownloaded":
      "веса ещё не скачаны — что дойдёт до движка, будет видно после развёртывания",
    "glossary.lost":
      "зачёркнутое движок выбросит: {dropped, plural, one {# фраза} few {# фразы} other {# фраз}} из {total}",
    "glossary.allReach": "все фразы дойдут до движка",
    "glossary.approximate": "; оценка приблизительная",
    "glossary.headNamed": "головы {variant}",
    "glossary.headAny": "выбранной головы",
    "glossary.missingSymbols": "Эти фразы содержат символы, которых нет в словаре {head}.",
    "glossary.missingList": "В словаре {head} нет {missing}.",
    "glossary.latinAdvice": " Пишите такие названия по-русски: опенвиспр вместо OpenWhispr.",
    "glossary.listSeparator": " и ",
    "glossary.example": "АйМоп|8",
    "glossary.latin": "латиницы",
    "glossary.digits": "цифр",
    "glossary.added": "добавлено, нажмите «Применить»",
    "glossary.duplicate": "уже в списке",
    "glossary.removed": "убрано, нажмите «Применить»",
    "glossary.applying": "применяю…",
    "glossary.applied": "движок перечитал глоссарий",
    "glossary.applyFailed": "не удалось применить",
    "glossary.imported":
      "из файла добавлено {added, plural, one {# фраза} few {# фразы} other {# фраз}}{skipped} — нажмите «Применить»",
    "glossary.importedSkipped": ", повторов пропущено {count}",
    "glossary.importedNothing": "новых фраз в файле не нашлось",
    "glossary.empty": "глоссарий пуст",
    "glossary.saved": "файл сохранён",
    "glossary.exportName": "глоссарий.txt",
    "glossary.envMissing":
      "В .env (INITIAL_CONTEXT) есть {count, plural, one {# фраза, которой нет} few {# фразы, которых нет} other {# фраз, которых нет}} в списке: {list}.",
    "glossary.envAdd": "Добавить из .env",
    "btn.apply": "Применить",
    "btn.import": "Импорт",
    "btn.export": "Экспорт",

    "section.connect": "Подключение",
    "connect.intro":
      "API совместим с OpenAI. Поле {prompt} движок принимает, но игнорирует — контекст задаётся глоссарием выше.",
    "connect.docs":
      "Схема управляющего API - {link}. Список моделей - {models}; поле {model} можно передавать любое, работает развернутая голова.",
    "connect.swagger": "Swagger на /api/docs",
    "snippet.python": "Python, openai SDK",
    "snippet.models": "список моделей",
    "btn.copy": "Скопировать",
    "btn.copied": "Скопировано",
    "snippet.file": "запись.wav",
    "snippet.noKey": "не-нужен",

    "section.logs": "Логи",
    "logs.follow": "следить",
    "foot.engine": "движок",
    "foot.model": "модель",

    "api.keyPrompt": "Сервис защищён ключом. Введите API_KEY из файла .env:",
    "api.error": "Ошибка {status}",

    "detail.deploy.downloading": "Проверяю и скачиваю модель ({variant})",
    "detail.deploy.starting": "Запускаю движок ({variant})",
    "detail.deploy.ready": "Готово: {variant}",
    "detail.download.retry": "{reason} Пробую снова ({attempt} из {attempts})…",
    "detail.download.failed": "{reason}",
    "detail.download.failedAttempts": " Попыток было {attempts}.",
    "detail.download.failedRunning": " Продолжает работать прежняя модель ({running}).",
    "detail.start.failed": "Не удалось запустить модель {variant}: {reason}",
    "detail.rollback.starting": "Откат на предыдущую модель ({variant})",
    "detail.rollback.done":
      "Откат на предыдущую модель ({variant}): модель {failed} не запустилась — {reason}",
    "detail.rollback.failed":
      "Модель {failed} не запустилась ({reason}), откат на {variant} тоже не удался.",
    "detail.glossary.restarting": "Перезапускаю движок для нового глоссария",
    "detail.glossary.restart_failed": "Не удалось перезапустить движок после правки глоссария",
    "detail.idle.autostart_off": "Автозапуск отключён (AUTOSTART=0)",
    "detail.idle.no_model": "Модель не выбрана — нажмите «Развернуть»",
    "detail.idle.stopped": "Движок остановлен",
    "detail.watchdog.exited": "Движок неожиданно завершился (код {code}), поднимаю заново",
    "detail.watchdog.restarting": "Поднимаю движок заново ({variant})",
    "detail.watchdog.ready": "Готово: {variant} (после перезапуска)",
    "detail.watchdog.failed": "Движок не поднялся (попытка {attempt}), пробую снова",
    "download.checksum":
      "Контрольная сумма скачанного файла не совпала — файл повреждён. Нажмите «Развернуть» ещё раз, битый файл будет перекачан.",
    "download.network":
      "Ошибка сети при скачивании модели. Проверьте доступ в интернет с сервера и попробуйте снова.",
    "download.disk":
      "Не удалось записать модель на диск. Проверьте свободное место и права на папку ./models.",
    "download.interrupted": "Скачивание модели прервано.",
    "download.other": "Скачивание модели не удалось.",

    "time.hours": "{hours} ч {minutes} мин",
    "time.minutes": "{minutes} мин",
    "time.seconds": "{seconds} с",
    "unit.seconds": "{value} с",
  },
};

/* Язык хранится у пользователя, а не у сервиса: консоль одна, а смотрят в неё
   разные люди. Настройка браузера намеренно не спрашивается: английский - язык
   по умолчанию всегда, русский включается в интерфейсе и запоминается. */
function storedLang() {
  let saved = null;
  try {
    saved = localStorage.getItem(LANG_STORAGE);
  } catch (err) {
    /* приватное окно: язык просто не запомнится */
  }
  return saved && LANGS.indexOf(saved) >= 0 ? saved : DEFAULT_LANG;
}

let lang = storedLang();

const currentLang = () => lang;

function setLang(next) {
  if (LANGS.indexOf(next) < 0 || next === lang) return false;
  lang = next;
  try {
    localStorage.setItem(LANG_STORAGE, lang);
  } catch (err) {
    /* см. выше */
  }
  return true;
}

/* Множественное число берется у платформы: в русском их три формы, в английском
   две, и писать свою таблицу ради этого незачем. Формат - подмножество ICU:
   {count, plural, one {…} few {…} other {…}}, где # - само число. */
const pluralRules = {};

function pluralForm(count, forms) {
  if (!pluralRules[lang]) pluralRules[lang] = new Intl.PluralRules(lang);
  const category = pluralRules[lang].select(count);
  const chosen = forms[category] || forms.other || "";
  return chosen.replace(/#/g, String(count));
}

function fill(template, params) {
  return template.replace(
    /\{(\w+)(?:,\s*plural,\s*((?:\w+\s*\{[^}]*\}\s*)+))?\}/g,
    (whole, name, plural) => {
      const value = params ? params[name] : undefined;
      if (!plural) return value === undefined || value === null ? whole : String(value);
      const forms = {};
      plural.replace(/(\w+)\s*\{([^}]*)\}/g, (_m, key, body) => {
        forms[key] = body;
        return "";
      });
      return pluralForm(Number(value) || 0, forms);
    }
  );
}

/* Перевод по ключу. Нет ключа в выбранном языке - берется английский, нет и там -
   возвращается сам ключ: так пропуск виден в интерфейсе, а не прячется. */
function t(key, params) {
  const template = (TEXT[lang] && TEXT[lang][key]) || TEXT[DEFAULT_LANG][key];
  if (template === undefined) return key;
  return fill(template, params);
}

/* Разметка помечена ключами, а не текстом: `data-i18n` меняет содержимое,
   остальные атрибуты - подпись, подсказку и имя для чтения с экрана. */
const ATTRS = [
  ["data-i18n", (node, value) => { node.textContent = value; }],
  ["data-i18n-placeholder", (node, value) => { node.placeholder = value; }],
  ["data-i18n-title", (node, value) => { node.title = value; }],
  ["data-i18n-aria", (node, value) => { node.setAttribute("aria-label", value); }],
];

function applyStaticTexts(root) {
  const scope = root || document;
  ATTRS.forEach(([attr, assign]) => {
    scope.querySelectorAll("[" + attr + "]").forEach((node) => {
      assign(node, t(node.getAttribute(attr)));
    });
  });
  document.documentElement.lang = lang;
  document.title = t("doc.title");
}

window.i18n = { t, currentLang, setLang, applyStaticTexts, LANGS };
})();
