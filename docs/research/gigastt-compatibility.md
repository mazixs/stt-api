# Совместимость Камертона с gigastt

Срез апстрима на **9 октября 2026**, повторная сверка — **20 ноября 2026**. Все часы публикации указаны в UTC. Поведение Камертона 1.5.0 сверено по `612a4557f7333e400df2a724964151e4ee6ff6a0`; исправления текущей ветки отмечены отдельно. Версию, хеши и диагностические сообщения работающего сервиса сверили только чтением. В локальных экспериментах не использовались рабочие тома, настройки или записи клиентов.

**Коротко.** Закреплённый `2.22.0` — последний Rust-релиз, обновлять версию движка пока не на что. 9 октября вышли новые **ONNX INT8** веса для двух RNN-T: `e2e_rnnt` уже проверен на нашем бинарнике, но решение о переходе требует сравнения точности, времени и памяти. У `ml_ctc_large` октябрьских весов нет. **GGUF `Q8_0`** существует для других движков, а `gigastt` его не загружает. **Диаризация** работает в нативном API `2.22.0`, но не выдаёт метки через OpenAI-совместимый запрос; в owner-интерфейсе это отдельная функция, а не скрытая настройка модели.

## Что именно вышло

| Дата | Что опубликовано / проверено | Что это значит для Камертона |
|---|---|---|
| 22.06.2026 | [Модельный пакет `models-v3-2026-06-22`][weights-old] | Источник RNN-T-энкодеров, встроенный в загрузчик закреплённого бинарника. |
| 04.10.2026, 12:07 | [Rust-релиз `v2.22.0`][release], коммит [`f4add25`][pin], образ `ghcr.io/ekhodzitsky/gigastt:2.22.0` | Версия в [Dockerfile](../../Dockerfile) и [Compose](../../docker-compose.yml). Это последний опубликованный Rust CLI и на 9 октября, и на дату повторной сверки. |
| 06.10.2026, 18:35 | [`node-v2.22.0`][node-release] — платформенные аддоны `.node` | GitHub API `/releases/latest` возвращает этот выпуск, но это npm, не обновление `gigastt serve`. Номер совпадает, исходный коммит отличается. |
| 07.10.2026, 22:09 | [В `main` восстановлен CPU ONNX Runtime для speaker encoder][diar-fix] | Исправление **после** Rust-тега; не входит в образ `2.22.0`. |
| 09.10.2026, 11:28 | [Выпущен `models-v3-2026-10-09`][weights-new] на коммите [`0e4ef04`][model-commit] | Пакет весов для `rnnt` и `e2e_rnnt`, не новый CLI. [Активация источника в `main`][activation] последовала в 11:58; старый бинарник сам на него не переключается. Для `ml_ctc` и `ml_ctc_large` новых весов в пакете нет. |
| 09.10.2026, 12:42 | [Последний коммит `main` на конец дня `47f5cf5`][oct-main] | [На шесть коммитов впереди Rust-тега][oct-compare], включая исправление диаризации и смену источника RNN-T. Не опубликованная версия. |
| 16.11.2026, 16:50 | [`main` на повторной сверке — `90fd360`][nov-main] | [Исходники впереди октябрьского снимка][nov-compare]; изменения ветки не следует приписывать `2.22.0`. |

Локальный OCI-образ сообщил `gigastt 2.22.0`; его `org.opencontainers.image.revision` совпал с Git-коммитом Rust-тега. Индекс, сверенный через локальный `docker image inspect` и публичный GHCR `docker buildx imagetools inspect`, — `sha256:ac7f7ccb7d5b9628d9e16cc361f29e61beedd8e1df8b0927b4a17921b7ec2f35`, манифест `linux/amd64` — `sha256:37a0141f8ab73ee62620bd0495989e8a2d76f6c9853a3165085cc76067516572`. Проект закрепляет **тег**, не digest; digest здесь фиксирует проверенный артефакт.

## Матрица `serve`, HTTP и UI

Колонка движка относится одновременно к **закреплённой** и **последней релизной** версии: обе `2.22.0`. Колонка фасада описывает Камертон 1.5.0; отличия текущей ветки приведены после таблицы.

| Узел | `gigastt serve` `2.22.0` | Камертон и применимость |
|---|---|---|
| Запуск и состояние | [Справка исходников][serve-args] и локальный бинарник принимают все 17 флагов, которые может собрать фасад: `--host`, `--port`, `--bind-all`, `--model-dir`, `--model-variant`, `--punctuation`, `--itn`, `--pool-size`, `--hotwords-file`, `--hotwords-boost`, `--body-limit-bytes`, `--punct-model-dir`, `--vad-model-dir`, `--file-window-concurrency`, `--vad`, `--hotwords-default`, `--enable-jobs`. Пул по умолчанию `2`, VAD выключен; `--execution-provider auto` выбирает провайдер сборки. | [engine.py][app-engine] передаёт явную голову, пул `1` по умолчанию, свой лимит multipart и каталоги sidecar-моделей. [state.py][app-state] хранит выбор головы и режимов; [Supervisor][app-supervisor] ставит сохранённое развёртывание выше `.env`. Отдельные ручки upstream `--batch-pool-size`, `--encoder-intra-threads`, `--optimized-cache-dir`, `--endpoint-mode` не являются полями UI/API Камертона. |
| Окна длинного файла | `--file-window-concurrency` по умолчанию `1`; для `2` нужен свободный слот, движок не ждёт его и не меняет поведение WS. [Опции пула и окон][pool-args]. | [API и UI][app-api] передают явный выбор. В исходном срезе есть разрыв только на чистом состоянии: `default_config()` собирает fallback без `FILE_WINDOW_CONCURRENCY` из `.env`; сохранённое или явно переданное значение применяется. Не трактовать настройку как общее ускорение для CTC. |
| Голова и оформление текста | Четыре головы: `rnnt`, `e2e_rnnt` (русский); `ml_ctc` (220 млн) и `ml_ctc_large` (600 млн) для ru/en/kk/ky/uz. Без явного `--model-variant` движок может определить установленную голову по каталогу. `auto/on/off` для русских RuPunct и ITN; `auto` включает отдельную обработку только для `rnnt`. У `e2e_rnnt` встроенное форматирование остаётся при `off`. [Правила режимов][text-modes]. | [Каталог][app-catalog] и UI дают четыре канонических ID, не переключают их полем OpenAI `model`. Подпись исходного `ml_ctc` о пунктуации «тем же проходом» неточна: `on` включает отдельную RuPunct, а `auto` — нет. Наличие файлов и настройка `on` сами по себе не доказывают, что модель пунктуации успешно загружена. |
| Форматы звука | [Декодирование по содержимому][audio-formats]: WAV (включая PCM/float, ADPCM, ряд телефонийных кодеков внутри WAV), MP3, M4A/AAC, FLAC, OGG/Vorbis, OGG/Opus, WebM/Opus. В `2.22.0` WebM/Opus ресемплируется с фактических **48 кГц** декодера, а не частоты заголовка. Raw `pcmu/pcma/g722` с `sample_rate` — синхронный native `/v1/transcribe`, не OpenAI, native SSE или jobs. | [Прокси][app-proxy] не перекодирует аудио, но пересобирает OpenAI multipart. Подсчёт RTF только для части WAV/WebM — это не список форматов, принимаемых движком; замер для сжатого WAV по PCM-формуле отдельно не подтверждён. [Браузер][app-ui] после Stop собирает WAV mono PCM16/16 кГц и отправляет `/api/test`, возвращающий `text`, `elapsed`, `audio_seconds`, `rtf`. |
| OpenAI-запрос и ответ | [Парсер и форматы][openai-parser]: `json` → `{text}`, также `text`, `srt`, `vtt`, `verbose_json`. Для `verbose_json` по умолчанию `segments`, параметр `timestamp_granularities[]` или `timestamp_granularities` может повторяться (`word` и `segment`), чтобы вернуть оба массива; у слов только `word/start/end`. `model` не переключает голову; `language` отражается в verbose, но не выбирает её. `prompt` и `temperature` игнорируются; `diarized_json` и перевод не предоставляются. | Фасад возвращает тело движка, сообщает об игнорируемых полях и отклоняет `/v1/audio/translations`. **Разрыв исходного среза:** пересборка из `dict` теряет повторные гранулярности и тем самым один массив. Исправление должно сохранять оба написания ключа; это контракт клиента, не причина менять движок. |
| SSE и живой поток | OpenAI `stream=true` только с `json`/`text`, после загрузки файла; `transcript.text.delta`, итог `transcript.text.done`, затем `[DONE]`. Итог может отличаться от склейки delta; при ошибке возможен SSE `error` без `[DONE]`. Native SSE — отдельные `partial/final/error`. Живой [`/v1/ws`, протокол 1.2][ws-protocol], принимает raw PCM16 LE (8/16/24/44,1/48 кГц), а не WAV/WebM. | OpenAI SSE передаётся. В исходном срезе HTTP 200 с дочитанным SSE `error` не отделён от успеха в метриках. Native SSE есть через локальный HTTP-проброс. Ни в локальном, ни в owner-профиле WS не проксируется: UI — файловая диктовка, не realtime-клиент. |
| Native HTTP, задачи, capabilities | [Native API][native-api] принимает raw body, возвращает `text/words/duration`, при наличии `confidence`, `segments` по запросу. Экспорт `txt/text`, `srt`, `vtt`, `md/markdown`. `punctuation/itn/vad` — булевы overrides; `variant` проверяет текущую голову, а не загружает другую. Jobs по `--enable-jobs`. Нативный `/v1/models` сообщает фактические encoder/provider/pool и доступность sidecar-возможностей. | [main.py][app-main] монтирует native catch-all только без owner-режима; jobs дополнительно требуют `ENABLE_JOBS=1`. `/v1/models` фасада — OpenAI-совместимый каталог, не копия ответа движка; `/api/status` прежде всего про настройки и фазу запуска. Для отображения фактических capabilities нужен отдельный контракт, а не изменение клиентского ответа. |
| Подсказки и глоссарий | [Файл + опциональный встроенный словарь][sidecars] объединяются; каскад токенизации [оригинал → строчные → `ё` в `е`][bias]. Подсказка меняет декодирование, не подставляет готовый текст. Неположительный вес фразы исключает её, положительный не является множителем общего буста; активный bias у CTC включает [prefix beam][ctc-bias]. Native query `hotwords`: отсутствует — серверный словарь, пусто — отключить, непусто — заменить на запрос (до 64 фраз по 64 символа; default boost `5`). У OpenAI `prompt` и у WS `configure` такого контракта нет. | Встроенный словарь в Камертоне включён по умолчанию; `INITIAL_CONTEXT` только инициализирует файл; правки UI перечитываются, при необходимости — через перезапуск. [vocab.py][app-vocab] проверяет представимость написания, не гарантирует активный bias или успешное распознавание: проверка по символам для BPE не равна токенизации движка и не учитывает все причины исключения фразы. Per-request `hotwords` доступен через локальный native, не через owner OpenAI. |

**Текущая ветка Камертона.** Исправлены оба разрыва 1.5.0: конфигурация первого запуска берётся из `.env` при пустом состоянии, а multipart сохраняет повторы **обеих** форм `timestamp_granularities` и `timestamp_granularities[]`, в том числе смешанную последовательность. Первое перепроверено от пустого `state` до `argv` (`POOL_SIZE=2`, `FILE_WINDOW_CONCURRENCY=2`), второе — тестами фасада. На реальном бинарнике напрямую оба написания с парой `word` + `segment` вернули оба массива. Подпись малой CTC-головы уточнена: отдельная обработка в `auto` выключена.

**Ветка разработки апстрима, не релиз.** На 9 октября [diff до `47f5cf5`][oct-compare] не меняет контракт CLI/HTTP/WS в этой матрице; меняются реализация диаризации и источник весов. В ноябрьском `90fd360` есть [WS 1.3 и `endpoint_silence_ms`][ws-nov] (40–2000 мс, default 600, для auto без VAD), [опциональный `--stream-ctc-model-dir`][ctc-confirm] для WS RNN-T, не для REST/SSE/jobs, и [WAV/G.726][g726]. Эти возможности не следует обещать для `2.22.0`.

### Диаризация: наличие модели, метки и интерфейс — разные вещи

| Путь | Что делает `2.22.0` | Что доступно в Камертоне |
|---|---|---|
| Модель и исполнение | [`wespeaker_resnet34.onnx`][speaker-model] (WeSpeaker, вход fbank) загружается лениво при первом запросе. [В теге][diar-code] speaker inference идёт через `FbankOnnxExtractor` из `polyvoice 1.0.0` (tract CPU), кластеризация — polyvoice. Коммит [от 7 октября][diar-fix] возвращает CPU ONNX Runtime именно для speaker encoder, сохраняя frontend, кластеризацию и ленивую загрузку; это не первая реализация диаризации. | Наличие файла/`diarization: true` говорит о потенциальной доступности, а не о том, что уже был запрос с метками. Наблюдение эксплуатации 20.11: `2.22.0`, `ml_ctc_large` дошла до `ready`, обнаружен WeSpeaker с сообщением о lazy load; выполнение диаризации из этой строки не следует. |
| Синхронный native / jobs | [Native `?diarization=true&segments=true`][native-api] добавляет целочисленный `speaker` к словам и сегментам. Если разметка недоступна, всё равно возможен HTTP 200 с текстом без `speaker` и объектом `diarization` с причиной. `channels=split` — разметка по настоящим двум каналам, а не кластеризация; оба режима вместе конфликтуют. Jobs возвращают нативную форму, если включены и запрошен соответствующий режим. Длительные whole-buffer пути ограничены примерно 30 минутами. | Локальный native HTTP-проброс доступен, owner не проксирует его. UI не предлагает настройку и отображение спикеров. Это ограничение интерфейса, не отсутствие диаризации в движке. |
| WebSocket / OpenAI | WS: `configure.diarization=true` при доступности в `ready`, метки могут появиться не сразу. Native SSE не делает диаризацию. OpenAI `/v1/audio/transcriptions` не возвращает `speaker` ни в `verbose_json`, ни при `stream=true`. [Сравнение схем][native-api]. | Для owner нужна отдельная продуктовая/API-доработка, если требуются метки; не открывать произвольный `/v1/*` ради одного метода. |

**Runtime 20.11.** На том же локальном OCI `2.22.0` с публичным WAV и [WeSpeaker с закреплённой HF-ревизии][speaker-model], SHA-256 `3955447b0499dc9e0a4541a895df08b03c69098eba4e56c02b5603e9f7f4fcbb`, нативный `/v1/models` сообщил `diarization: true`. Native `POST /v1/transcribe?diarization=true&segments=true` вернул `speaker: 0` у 7 слов и сегмента. OpenAI `verbose_json` с таймкодами слов на том же файле вернул слова без `speaker`. Это проверка прохождения поля для одной реплики одного спикера, не оценка числа кластеров, DER, скорости диаризации или WS. [Контроль апстрима от 7 октября][diar-evidence] показал отношение медиан tract/исправленный ORT **7,11×** на стадии получения эмбеддингов (Ryzen AI 9 HX 370; девять клипов, семь раундов, ORT 6 intra-op-потоков). В измерение не входят ASR, загрузка, декодирование и кластеризация; это не ускорение полного REST-запроса.

## Форматы и источники весов

**GGUF `Q8_0` и ONNX INT8 — не взаимозаменяемые форматы.** В официальном `gigastt serve` `2.22.0` нет загрузчика GGUF, бэкенда `ggml` и переключателя `Q8_0`: [продуктовый загрузчик][load-files] требует ONNX с INT8-энкодером; в архивном [Candle-плане][gguf-plan] GGUF числится задачей, а не возможностью ASR. Октябрьский выпуск меняет схему квантизации **ONNX**: свёртки остаются FP32, веса подходящих MatMul/Gemm квантизуются в INT8 по выходным каналам. Это не FP32-режим сервера и не GGUF.

| Вариант | Формат и источник | Загрузка и применимость |
|---|---|---|
| `rnnt` и `e2e_rnnt` | `v3_{rnnt,e2e_rnnt}_{encoder_int8,decoder,joint}.onnx`, словари `v3_vocab.txt` / `v3_e2e_rnnt_vocab.txt`; [июньские GitHub assets][weights-old] для `2.22.0`, [октябрьские][weights-new] для новых свежих загрузок из снимка `main` после #437. FP32 ONNX для **сборки весов** — [HF `istupakov/gigaam-v3-onnx`, `322c3b2`][hf-rnnt]; исходные и выходные хеши в [манифесте релиза][weights-manifest]. | Два разных источника и два набора байтов под теми же именами. Уже полный набор используется с диска; [галочка «скачано»][app-catalog] проверяет имена/ненулевой размер, но не различает ревизии. Официальный релиз также даёт offline-архивы Linux и пакет `gigastt-model-int8_2.22.0_all.deb`, не GGUF. |
| `ml_ctc`, `ml_ctc_large` | `multilingual_ctc.int8.onnx` и `multilingual_large_ctc.int8.onnx`, общий `multilingual_vocab.txt`; [HF `458860e`][hf-ctc] и [HF `07665ab`][hf-ctc-large]. [Код загрузчика][ctc-assets] запрашивает `resolve/main`, но сравнивает контент с закреплёнными SHA-256. | Для каждого варианта один CTC-энкодер, без RNN-T decoder/joint. Зафиксированные здесь HF-коммиты подтверждают источники, но не означают, что URL в бинарнике содержит эти коммиты. Октябрьский RNN-T выпуск их не меняет. |
| Дополнительные модели | [RuPunct INT8][hf-punct] (`rupunct_small_int8.onnx`, `tokenizer.json`, `config.json`), [Silero VAD v5.1.2][hf-vad] (`silero_vad.onnx`), [WeSpeaker][speaker-model] (`onnx/model.onnx` → `wespeaker_resnet34.onnx`). [Загрузчик sidecar][sidecar-assets] отдельно проверяет SHA-256. | Это не ASR-головы и не GGUF. WeSpeaker загружается при штатном `gigastt download`, если не передан `--skip-diarization`; пунктуация/VAD разрешаются отдельными режимами и каталогами. |
| Другие сборки и провайдеры | [ORT `auto/cpu/coreml/cuda`][ort-selection] — выбор провайдера ONNX Runtime, а не формата. Экспериментальный Candle использует FP32 `.safetensors`; Apple ANE — [отдельный `.mlpackage`-пакет][ane-release] для энкодера, с FP16 вычислениями и 6-битной палетизацией, остальное на ORT. [Выбор бэкенда][backend-code] ограничивает Candle/ANE вариантом `rnnt`. | В данном образе Камертон использует CPU; другой провайдер или бэкенд требует соответствующей сборки. Ни один из этих путей не даёт загрузку GGUF в штатном `serve`. |
| Ноябрьские исходники `main` | [Локальные INT8-пакеты][local-packs] и [явный FP32 E2E RNN-T на CPU][fp32-main]; FP32 только с явно выбранным форматом/пакетом, нет неявного fallback из INT8. | Не возможности выпущенного `2.22.0`. Свежая загрузка по умолчанию остаётся INT8; локальный `manifest.toml` в самом релизе не включает FP32-модель. |

На 9 октября **существовали сторонние GGUF**: карточки HF от автора `handy-computer` с ревизиями от **15.09.2026** содержат [`gigaam-v3-rnnt-Q8_0.gguf`][gguf-rnnt] и [`gigaam-v3-e2e-rnnt-Q8_0.gguf`][gguf-e2e] для `transcribe.cpp`. Это другой runtime, а не release assets `ekhodzitsky/gigastt`. Эти файлы нельзя поставить в каталог Камертона вместо ONNX; перенос потребовал бы другого движка и отдельной сверки CLI, контрактов и качества.

## Пакет RNN-T от 9 октября: совместимость проверена отдельно

В GitHub Releases у июньского и октябрьского пакета одинаковые имена файлов, но разные SHA-256 энкодеров. Сопоставление с инвентаризацией установленных весов 20.11 показывает, что используемые RNN-T-энкодеры соответствуют **июньскому** выпуску; октябрьский в этой инвентаризации не обнаружен. Энкодеры `ml_ctc` (`e08e27ae…`) и `ml_ctc_large` (`b2ad9c38…`) совпали с [контрольными суммами релизного загрузчика][ctc-assets].

| Энкодер | 22 июня — 225 250 603 байта (≈215 МиБ) | 9 октября — 319 665 270 байт (≈305 МиБ) |
|---|---|---|
| `v3_rnnt_encoder_int8.onnx` | `c52665e9d96c4ca3a153c063d2ee9af6c567fe2975ca50fd038b75bbf2f60e7f` | `1d5a6f580b692e38ba35ed98d0f54648808f27b0a147dc11bd8978c15dc1a18f` |
| `v3_e2e_rnnt_encoder_int8.onnx` | `cf51b300af47cea099e17c806f8fecce2c46e9e8deb4709ec203f8970a067389` | `16805903b102d8b27bb0253de044f25e33a8a5bf07fce0c4da4ac1a5a7544091` |

Проверены два отдельных каталога `e2e_rnnt`: в каждом энкодер, декодер, joint и словарь; все 4/4 SHA-256 совпали с [метаданными релизов][weights-old]. Декодер, joint и словарь в этих двух комплектах побайтово одинаковы. Один [публичный `golos_00.wav`][golos-fixture] (SHA-256 `500d2e88a1634ee766fec46f83cb903f3a1c276b2cedb36f11db800a1c4c14d0`) распознан **обоими** комплектами в официальном `2.22.0` офлайн: `transcribe` завершился с кодом `0`; `serve` вернул `2xx` и непустое поле `text` через `/v1/audio/transcriptions`. Текст совпал в обоих A/B. В `serve` использовались CPU, 4 intra-op-потока, пул 1 и выключенные RuPunct/ITN; порядок готовности — успешный `/v1/models`, поскольку `/health` доступен уже при загрузке модели. Контейнеры не имели сети за пределами loopback, рабочих томов или опубликованных портов.

Это подтверждает, что октябрьский `e2e_rnnt` загружается без смены закреплённого движка и выполняет базовый REST-запрос. Это **не** проверка полной точности, ускорения, второй головы `rnnt` или совместимости каждого режима запуска. Для интеграции нужен отдельный явный выбор ревизии/пакета с проверкой SHA-256 и возможностью отката; не следует менять содержимое рабочего каталога под теми же именами незаметно для пользователя.

## Скорость: что подтверждают наши замеры

RTF — время обработки, делённое на длительность звука; меньше — быстрее. Данные ниже из [«Выбора головы и WER»][our-speed], а не новый бенчмарк `2.22.0` или октябрьского пакета.

| Дата, версия и режим | Наблюдение | Как читать |
|---|---|---|
| 11.08, `2.17.0`, настольный CPU с 16 ядрами, пул 1 | На 165 с звука `e2e_rnnt` — 6,6 с, `ml_ctc_large` — 11,19 с. На 300 фразах FLEURS WER 4,32% для `e2e_rnnt`, 6,25% для `ml_ctc_large` с RuPunct и ITN. | Сравнение сценария русской диктовки. Это не замер английского, казахского, кыргызского или узбекского. |
| 04.09, `2.20.0`, `e2e_rnnt`, пул 1, 16 ядер | RTF **0,031–0,040** на наборе 9,2–615,8 с; накладные локального фасада — 5–25 мс в пределах точности логов. VAD: на двух длинных записях −21,5% и −29,5% времени, но 3,07% и 3,44% расхождения со словами прогона без VAD; на короткой +19% времени. | Ускорение за счёт пауз не оправдало смену умолчания VAD; расхождение с другим прогоном не равно WER к человеческой разметке. |
| 06.09, `2.21.0`, `e2e_rnnt`, 16 ядер | Пул/окна `2/2` против `1/1`: 55,2 → 39,7 с (**−28,1%**) на семи файлах, ноль различий после нормализации 2074 слов, пик 264 → 453 МиБ. Пул `2` с одним окном: +4,5%; два окна при пуле `1` — без подтверждённого выигрыша. | Два окна ускоряют длинный файл только при доступном втором слоте. Память измерялась отдельно по режимам. |
| 06.09, `2.21.0`, `ml_ctc_large` | Тот же переход `1/1` → `2/2`: **+12,3%** времени на 16 ядрах; на 8 ядрах — **+5,1%**. | Эффект `e2e_rnnt` не переносится на `ml_ctc_large`. |
| 18.11, `2.22.0`, отдельный [локальный smoke][our-smoke] | `rnnt` на WAV ≈4 с: 0,382 с, RTF 0,096; на склейке WAV 61,765 с: 4,746 с, RTF 0,077. | Одиночные проверки работоспособности на других данных и окружении; не сравнение скорости релизов. |

### Контроль текущих весов, 20.11.2026

Отдельный локальный `gigastt serve --offline` версии `2.22.0`, Linux в Docker на Intel Core i7-11700K, лимит 4 CPU / 4 ГиБ, CPU ORT с 4 intra-op-потоками, пул и число окон — 1, RuPunct/ITN/VAD выключены. Один синтетический WAV Windows TTS `ru-RU` (22,05 кГц, 6,079 с; SHA-256 `2c39a132fa32343d37328487e05963e5e8c31b3db2f0b63cbaadb40b7e2f5a87`), по одному каталогу на голову, одно постоянное локальное HTTP-соединение к `/v1/audio/transcriptions`, 2 прогрева и 9 последовательных запросов на голову. Все ответы содержали текст. Проверка не проходила через фасад и не оценивала WER.

| Голова | Медиана HTTP, мс | Мин–макс, мс | RTF по медиане |
|---|---:|---:|---:|
| `rnnt` | 472,7 | 454,1–601,7 | 0,078 |
| `e2e_rnnt` | 438,6 | 403,0–539,0 | 0,072 |
| `ml_ctc` | 309,2 | 250,0–336,9 | 0,051 |
| `ml_ctc_large` | 601,8 | 528,7–789,4 | 0,099 |

Это одна короткая фраза, не SLA и не задержка рабочего сетевого маршрута; порядок голов не следует переносить на другие длины, другие CPU или языки. Октябрьские веса в этом замере не участвовали.

### Замеры октябрьского пакета у апстрима

Октябрьский пакет проверен **апстримом**, а не нашим бенчмарком: [публичный long-form JSON][long-evidence] — семь записей, 1154 с, 2714 слов, Linux x86-64 Ryzen AI 9 HX 370, 6 потоков, одна сессия, последовательные окна. Медианное отношение прогретого времени к пакету июня — **0,547×** для `rnnt`, **0,560×** для `e2e_rnnt`; пик RSS — **1,233×** и **1,221×** соответственно. На long-form ошибка в словах снизилась с 200 до 197 и со 160 до 158 соответственно. В [коротком Golos A/B][short-evidence] (992 непустых эталона) нормализованный `rnnt` — 139 → 138 ошибок (2,937% → 2,916%), `e2e_rnnt` — 397 → 399 (8,390% → 8,432%); исходная, ненормализованная оценка `e2e_rnnt` — 756 → 762 ошибки. Это измерение конкретных весов, корпуса и CPU, не обещание ускорения на Камертоне. Апстрим отдельно приводит частный репорт: его материал здесь не использовался.

## Методика и воспроизведение

1. **Обнаружение релизов.** Сверить [ленточный GitHub Releases API][releases-api], [`/releases/latest`][latest-api], публичные теги и peeled commit аннотированного Rust-тега. Для исторической ветки взять последний коммит до `2026-10-09T23:59:59Z` ([запрос][oct-query]). Для настоящей ветки зафиксирован SHA, а не плавающий `main`.
2. **Проверка фактического CLI.** В локальном Docker `linux/amd64` запущены `gigastt --version` и `gigastt serve --help` из проверенного образа, с `--pull never --network none --read-only`, без томов и портов. Сигнатура флага и фактическая HTTP-семантика — разные проверки: серверные ветки читаются по Rust-коммиту, фасад — по файлам проекта, runtime отмечен отдельно. Справка CLI может отставать от реализации.
3. **Проверка файлов весов.** Имена и SHA-256 сверяются по метаданным GitHub Releases; исходники указывают порядок поиска и загрузки. [Model Release][build-proof] и [CI][ci-proof] завершились успешно для того же коммита `0e4ef04`. Это проверка метаданных и контрольных сумм, не отдельная проверка подписи minisign и не перепаковка весов.
4. **Повторяемый A/B для решения об интеграции.** Один и тот же бинарник, публичные аудио и эталоны, отдельные каталоги моделей/ORT-кеша, одинаковые provider, head, потоки, пул, VAD, ITN, пунктуация и глоссарий. Сначала контроль текста WAV и WebM/Opus одного звука (проверять непустой текст, не только HTTP `200`), затем отдельно короткий и длинный корпус. Разделять холодный старт/компиляцию графа и прогретый wall time; повторять в чередующемся порядке, считать разброс, RTF и пик cgroup/RSS отдельно. Проверять сырой и нормализованный WER, серии выпадений слов и ошибки написания терминов. Расхождение между версиями без размеченного эталона — не WER качества.

Минимальный локальный REST-smoke: в отдельном контейнере без опубликованных портов смонтировать **один** из каталогов проверенных весов только для чтения в `/models`. Запустить `gigastt serve --offline --host 127.0.0.1 --port 9876 --model-dir /models --model-variant e2e_rnnt --execution-provider cpu --pool-size 1 --encoder-intra-threads 4 --punctuation off --itn off`; после успешного `/v1/models` отправить из того же контейнера публичный файл:

```sh
curl -fsS --form file=@golos_00.wav --form-string response_format=json \
  http://127.0.0.1:9876/v1/audio/transcriptions
```

Для проверки меток в такой же отдельный каталог добавить WeSpeaker с указанным SHA-256 и выполнить нативный запрос. Без `speaker` в ответе или при `diarization.status=unavailable` один HTTP `200` не считается подтверждением.

```sh
curl -fsS -H 'Content-Type: application/octet-stream' --data-binary @golos_00.wav \
  'http://127.0.0.1:9876/v1/transcribe?diarization=true&segments=true'
```

**Что имеет смысл интегрировать.** Для русской диктовки проверить октябрьский `e2e_rnnt` отдельным A/B на целевом CPU; при выигрыше добавить явный выбор ревизии весов, не замену файлов в существующем каталоге. Для текущей `ml_ctc_large` этот выпуск не повод переключать модель. Если нужны метки говорящих в owner-режиме, отдельно спроектировать ограниченный маршрут выдачи и проверять его на публичном корпусе с несколькими спикерами; при выборе новой версии заново сверить CPU-бэкенд диаризации после выхода Rust-релиза.

[weights-old]: https://github.com/ekhodzitsky/gigastt/releases/tag/models-v3-2026-06-22
[weights-new]: https://github.com/ekhodzitsky/gigastt/releases/tag/models-v3-2026-10-09
[release]: https://github.com/ekhodzitsky/gigastt/releases/tag/v2.22.0
[pin]: https://github.com/ekhodzitsky/gigastt/commit/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed
[node-release]: https://github.com/ekhodzitsky/gigastt/releases/tag/node-v2.22.0
[diar-fix]: https://github.com/ekhodzitsky/gigastt/commit/ef7c3f854b5d0633f09a26433172bd5f6d9bbf7c
[model-commit]: https://github.com/ekhodzitsky/gigastt/commit/0e4ef04c332a6b0229948df8b4a148645cad3a17
[activation]: https://github.com/ekhodzitsky/gigastt/pull/437
[oct-main]: https://github.com/ekhodzitsky/gigastt/commit/47f5cf55e926a397d328978aec3441d955ab9483
[nov-main]: https://github.com/ekhodzitsky/gigastt/commit/90fd3607f5e9ad84c5a7896f8d209b4fb5475309
[oct-compare]: https://github.com/ekhodzitsky/gigastt/compare/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed...47f5cf55e926a397d328978aec3441d955ab9483
[nov-compare]: https://github.com/ekhodzitsky/gigastt/compare/47f5cf55e926a397d328978aec3441d955ab9483...90fd3607f5e9ad84c5a7896f8d209b4fb5475309
[our-speed]: head-choice-and-wer.md
[our-smoke]: ../manual-smoke.md#результат-проверки-gigastt-2220
[releases-api]: https://api.github.com/repos/ekhodzitsky/gigastt/releases
[latest-api]: https://api.github.com/repos/ekhodzitsky/gigastt/releases/latest
[oct-query]: https://api.github.com/repos/ekhodzitsky/gigastt/commits?sha=main&until=2026-10-09T23%3A59%3A59Z&per_page=1
[long-evidence]: https://github.com/ekhodzitsky/gigastt/blob/0e4ef04c332a6b0229948df8b4a148645cad3a17/benchmark/results/rnnt-quantization/2026-10-09/summary.json
[short-evidence]: https://github.com/ekhodzitsky/gigastt/blob/0e4ef04c332a6b0229948df8b4a148645cad3a17/benchmark/results/rnnt-quantization/2026-10-09/golos-report.json
[build-proof]: https://github.com/ekhodzitsky/gigastt/actions/runs/37918723937
[ci-proof]: https://github.com/ekhodzitsky/gigastt/actions/runs/37918705864
[golos-fixture]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/tests/fixtures/golos_00.wav
[serve-args]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/src/serve.rs#L38-L279
[pool-args]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/src/serve.rs#L207-L279
[text-modes]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/src/boot/modes.rs#L24-L93
[audio-formats]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/docs/api.md#L823-L874
[openai-parser]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/src/server/openai/mod.rs#L180-L264
[ws-protocol]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/protocol/mod.rs#L5-L124
[native-api]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/docs/api.md#which-endpoint-returns-which-fields
[sidecars]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt/src/boot/sidecars.rs#L122-L169
[bias]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/inference/bias.rs#L30-L51
[ctc-bias]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/inference/engine/infer.rs#L68-L96
[ws-nov]: https://github.com/ekhodzitsky/gigastt/blob/90fd3607f5e9ad84c5a7896f8d209b4fb5475309/docs/api.md#L185-L197
[ctc-confirm]: https://github.com/ekhodzitsky/gigastt/blob/90fd3607f5e9ad84c5a7896f8d209b4fb5475309/docs/stream-ctc-confirmation.md#L34-L49
[g726]: https://github.com/ekhodzitsky/gigastt/blob/90fd3607f5e9ad84c5a7896f8d209b4fb5475309/docs/api.md#L830-L839
[speaker-model]: https://huggingface.co/onnx-community/wespeaker-voxceleb-resnet34-LM/blob/6a61a1833ff2583aabeba044f5c8221f00b67ceb/onnx/model.onnx
[app-engine]: ../../console/engine.py
[app-state]: ../../console/state.py
[app-supervisor]: ../../console/supervisor.py
[app-api]: ../../console/api.py
[app-catalog]: ../../console/catalog.py
[app-proxy]: ../../console/proxy.py
[app-ui]: ../../console/static/app.js
[app-main]: ../../console/main.py
[app-vocab]: ../../console/vocab.py
[gguf-rnnt]: https://huggingface.co/handy-computer/gigaam-v3-rnnt-gguf/blob/bbaea47d3e7b1bfa702ac050127cd93ff778837d/gigaam-v3-rnnt-Q8_0.gguf
[gguf-e2e]: https://huggingface.co/handy-computer/gigaam-v3-e2e-rnnt-gguf/blob/b9b68a835993df09018237a11d2a9b8c1925844d/gigaam-v3-e2e-rnnt-Q8_0.gguf
[load-files]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/inference/load_files.rs#L65-L112
[gguf-plan]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/docs/archive/candle-metal-backend-plan.md#L678-L686
[hf-rnnt]: https://huggingface.co/istupakov/gigaam-v3-onnx/tree/322c3b29492673eb7d0b434bfa9dfb8653e34d02
[weights-manifest]: https://github.com/ekhodzitsky/gigastt/blob/0e4ef04c332a6b0229948df8b4a148645cad3a17/benchmark/model-release.json#L1-L25
[hf-ctc]: https://huggingface.co/istupakov/gigaam-multilingual-ctc-onnx/tree/458860e1983aef670dd9795fb6af603c82767d5d
[hf-ctc-large]: https://huggingface.co/istupakov/gigaam-multilingual-large-ctc-onnx/tree/07665ab5e54371dd1ac7b8b10f06478003723573
[ctc-assets]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/model/variant.rs#L156-L232
[hf-punct]: https://huggingface.co/ekhodzitsky/rupunct-small-onnx/tree/c4a35be7e4a413cdfb08427db75b621b571569c9
[hf-vad]: https://github.com/snakers4/silero-vad/blob/v5.1.2/src/silero_vad/data/silero_vad.onnx
[sidecar-assets]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/model/download/sidecars.rs
[ort-selection]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/runtime/ort/selection.rs#L9-L43
[ane-release]: https://github.com/ekhodzitsky/gigastt/releases/tag/ane-v3-2026-06-24
[backend-code]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/runtime/ort/factory.rs#L272-L288
[local-packs]: https://github.com/ekhodzitsky/gigastt/pull/444
[fp32-main]: https://github.com/ekhodzitsky/gigastt/blob/90fd3607f5e9ad84c5a7896f8d209b4fb5475309/docs/fp32-cpu.md
[diar-code]: https://github.com/ekhodzitsky/gigastt/blob/f4add258610c3b1c3971e6e0e5e3ab3bfac818ed/crates/gigastt-core/src/inference/diarization.rs#L1-L230
[diar-evidence]: https://github.com/ekhodzitsky/gigastt/blob/ef7c3f854b5d0633f09a26433172bd5f6d9bbf7c/benchmark/results/diarization-backend/2026-10-07.json
