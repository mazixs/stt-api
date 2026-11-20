# Ручная проверка на живом движке

Автотесты подменяют движок заглушкой и ничего не качают. Этот сценарий гоняет
настоящий gigastt с настоящими весами GigaAM v3. Занимает 10–15 минут, из них
большую часть — скачивание модели.

Команды сценария выполняются из корня отдельной копии проекта со свежими `data/`
и `models/`: он разворачивает голову, меняет глоссарий и завершает процесс движка.
Не запускайте его с рабочими томами. Цифры в шаге 3 — замер от 29.07.2026; на другом
железе сравнивайте поведение, а не точные значения.

## Ручная установка и обновление: проверка 20.11.2026

Инструкция для установки — [здесь](install.ru.md). Проверяли на отдельных проектах Docker с тестовыми паролями, ключами и каталогами, не на рабочей установке.

| Сценарий | Что проверено |
|---|---|
| Один YAML для внешнего прокси | В пустом каталоге оставили только NPM-манифест и закрытый `.env`, без `Dockerfile`. Compose 2.24.6 собрал приложение из Git по тегу `v1.6.0`. Переход с образа 1.5.0 на 1.6.0 сохранил порт, пути данных, вход владельца и ранее выданный клиентский ключ. Это проверка того, что получит Compose после синхронизации файлов, **не** запуск Sync на сервере Arcane. |
| Граница HTTPS | Через настоящий nginx проверили страницу входа, отказ без ключа, создание и отзыв ключа. Через встроенный Caddy повторили вход и запрос со старым ключом после `update --version v1.6.0`; корневой сертификат остался прежним. Публичный DNS и выпуск сертификата ACME в этих опытах не участвовали. |
| Резервная копия | После `backup` сервис снова работал. Из тестовой копии отдельно открыли SQLite: проверка целостности прошла, запись владельца и клиентского ключа присутствовали. В архив Caddy вошли сертификат и закрытый ключ тестового CA. |
| Откат неудачного обновления | Запустили настоящую `rnnt`, затем попытались обновиться на тестовый образ, у которого веб-сервис отвечает, но движок не запускается. CLI завершился с ошибкой, вернул старый образ, дождался `ready` у `rnnt`; исходный API-ключ по-прежнему принимался. |

## Результат проверки gigastt 2.22.0

18.11.2026, Docker Desktop `linux/amd64`, CPU, лимит контейнера 3 ГиБ. Образ
консоли собран на `debian:trixie-slim` с официальным
[`ghcr.io/ekhodzitsky/gigastt:2.22.0`](https://github.com/ekhodzitsky/gigastt/releases/tag/v2.22.0)
(индекс OCI `sha256:ac7f7ccb7d5b9628d9e16cc361f29e61beedd8e1df8b0927b4a17921b7ec2f35`).
Контейнеры не публиковали порты и не использовали рабочие тома.

- CLI сообщил `gigastt 2.22.0`, `serve --help` содержит все 17 флагов из
  `console/engine.py`. `rnnt`, `e2e_rnnt`, `ml_ctc` и `ml_ctc_large` скачались,
  прошли проверку SHA-256 и распознали русскую запись через `/api/test`. Консоль
  отметила все четыре головы как скачанные. На `e2e_rnnt` в `argv` проверены
  `--vad`, `--pool-size 2` и `--file-window-concurrency 2`.
- На `rnnt` прошли JSON, текст, SRT, VTT, `verbose_json` со словами, SSE с
  совпадающим итоговым текстом и `[DONE]`, нативный `/v1/transcribe` и задача
  `/v1/jobs` при `ENABLE_JOBS=1`. `prompt` помечен как игнорируемый. Без ключа
  `/v1/models` вернул `401`; с ключом — `200`, как и `/health` без ключа.
- Для WAV из [набора Golos в апстриме](https://github.com/ekhodzitsky/gigastt/tree/v2.22.0/crates/gigastt/tests/fixtures)
  `rnnt` на 4-секундной записи показал 0,382 с (RTF 0,096), на склейке 15 файлов
  длительностью 61,765 с — 4,746 с (RTF 0,077). `e2e_rnnt` с указанными выше
  флагами также распознал длинный WAV до последней фразы. CDN примеров из шага 3
  не использовали: сертификат HTTPS был просрочен.
- На том же коротком WAV оба WebM/Opus (заголовки 16 и 48 кГц, пакеты 60 мс)
  дали текст WAV напрямую и через консоль. На контрольной 2.21.0 вместо
  «60000 тенге, сколько будет стоить?» WebM с заголовком 16 кГц вернул «Да.».
- При `MAX_UPLOAD_MB=3` ровно 3 МиБ приняли с `200`, 3 МиБ + 2 байта отклонили
  с `413`. Глоссарий перечитан без смены PID. После завершения дочернего процесса
  сторож вернул `ready` с `restart_count=1`; после перезапуска контейнера из
  `state.json` поднялся `ml_ctc_large`. `cache-gc` сохранил четыре графа,
  распознавание после уборки работало.

## 1. Сборка и запуск

```sh
cp .env.example .env
docker compose up -d
docker compose ps                       # ожидается: Up (healthy)
curl -s localhost:8091/health
```

Ожидается `{"status":"ok","engine_status":"stopped",...}` — консоль жива, модель
не развёрнута. Проверьте, что `engine_detail` равен `Движок остановлен` и в ответе
есть `engine_detail_code`. Тексты ошибок и локальные пути не должны попадать в
`/health`; подробности смотрите в поле `detail` ответа `/api/status`.
Проверьте, что бинарник движка совместим с базовым образом:

```sh
docker compose run --rm --entrypoint /usr/local/bin/gigastt stt-api --version
# gigastt <версия из GIGASTT_TAG в docker-compose.yml>
```

## 2. Развёртывание модели

```sh
curl -s -X POST localhost:8091/api/deploy \
  -H 'content-type: application/json' -d '{"variant":"rnnt"}'

# наблюдать прогресс
watch -n5 "curl -s localhost:8091/api/status | python3 -m json.tool | head -5"
```

Ожидается последовательность `downloading` (с растущим `download_percent`) →
`starting` → `ready`. Скачивается ~230 МБ весов плюс ~31 МБ модели пунктуации.
В веб-консоли на `http://localhost:8091` откройте «Настройки запуска» и проверьте
текущий этап развёртывания. Пока операция идёт, повторное развёртывание недоступно.
«Журнал» показывает подтверждённые события сервиса, а вывод движка находится в
свёрнутом блоке «Технические логи».

Если скачивание обрывается (обрыв TLS на большом файле), сервис делает два
повтора сам. Ручной обход — раздел «Если что-то пошло не так» в README.

## 3. Распознавание

Официальные примеры GigaAM:

```sh
curl -sLO https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav       # 11.3 с
curl -sLO https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/long_example.wav  # 71.2 с

curl -s localhost:8091/v1/models    # один id: kamerton; variant совпадает с /api/status
curl -s -X POST localhost:8091/v1/audio/transcriptions -F model=kamerton -F file=@example.wav
curl -s -X POST localhost:8091/v1/audio/transcriptions -F model=whisper-1 -F file=@example.wav  # совместимость
curl -s -X POST localhost:8091/api/test -F file=@example.wav
curl -s -X POST localhost:8091/api/test -F file=@long_example.wav
```

Ожидается связный русский текст с пунктуацией и заглавными буквами. Замеры,
полученные при проверке:

| Файл | Аудио | Обработка | RTF |
|---|--:|--:|--:|
| `example.wav` | 11.29 с | 0.38 с | 0.034 |
| `long_example.wav` | 71.25 с | 2.82 с | 0.040 |

Проверьте форматы ответа и потоковую отдачу:

```sh
curl -s -X POST localhost:8091/v1/audio/transcriptions -F file=@example.wav -F response_format=srt
curl -sN -X POST localhost:8091/v1/audio/transcriptions -F file=@example.wav -F stream=true | head -5
curl -s localhost:8091/v1/models | python3 -m json.tool
```

В потоке на файловой загрузке проверяйте `transcript.text.done` и завершающий
`[DONE]`, а не число дельт. Итоговый текст должен совпадать с обычной загрузкой
(на движке до 2.19.0 расходился, см. [замер](research/head-choice-and-wer.md)).

Отдельно проверьте WebM/Opus с 60-мс пакетами. До 2.17.0 этот формат мог получать
`422`, а до 2.22.0 файл с заголовком 16 кГц мог получать `200` с неверным или
пустым текстом. Берите настоящую речь и проверяйте текст, а не только код ответа:

```sh
for rate in 16000 48000; do
  ffmpeg -y -i example.wav -ar "$rate" -c:a libopus -frame_duration 60 -f webm "speech-$rate.webm" || exit 1
  python3 -I - "speech-$rate.webm" "$rate" <<'PY' || exit 1
import struct, sys
with open(sys.argv[1], 'rb') as file:
    header = file.read(4096)
rate = int(sys.argv[2])
if not any(bytes((0xB5, 0x80 + size)) + struct.pack(fmt, rate) in header
           for size, fmt in ((4, '>f'), (8, '>d'))):
    sys.exit(f'В заголовке WebM нет SamplingFrequency={rate}')
PY
  curl -fsS -X POST localhost:8091/api/test -F "file=@speech-$rate.webm"
  docker compose cp "speech-$rate.webm" "stt-api:/tmp/speech-$rate.webm"
  docker compose exec stt-api curl -fsS -X POST 127.0.0.1:9876/v1/audio/transcriptions \
    -F "file=@/tmp/speech-$rate.webm"
done
```

Для обоих вариантов ожидается непустой текст, совпадающий с WAV.

Консоль такие загрузки не переписывает — она только читает их длину, чтобы посчитать
RTF (`console/webminfo.py`), потому что в ответе формата `json` длительности нет.

## 3.5. Лимит размера

Файл ровно в `MAX_UPLOAD_MB` должен распознаваться, а файл больше — получать `413` от
консоли с текстом про размер. Ответ `400 Invalid multipart body` означает, что движку
не передали предел тела (см. [решения](decisions.md)):

```sh
MB=${MAX_UPLOAD_MB:-150} python3 -c "
import os, struct, pathlib
n = (int(os.environ['MB'])*1024*1024 - 44) // 2
d = bytes(n*2)
h = b'RIFF' + struct.pack('<I', 36+len(d)) + b'WAVEfmt ' + struct.pack('<IHHIIHH',16,1,1,16000,32000,2,16) + b'data' + struct.pack('<I', len(d))
pathlib.Path('limit.wav').write_bytes(h+d)"

curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8091/v1/audio/transcriptions \
  -F model=kamerton -F file=@limit.wav                       # ожидается 200

docker compose exec stt-api sh -c 'ps -eo args | grep "[g]igastt serve"' \
  | tr ' ' '\n' | grep -A1 body-limit             # ожидается (MB + 1) * 1048576
```

## 4. Глоссарий без перезапуска

**Этот шаг перезаписывает глоссарий целиком, а не дописывает к нему.** На рабочей
установке сначала заберите копию, иначе весь список заменится двумя фразами:

```sh
curl -s localhost:8091/api/glossary | python3 -c "import json,sys; print(json.load(sys.stdin)['text'])" > hotwords.bak
wc -l hotwords.bak
```

```sh
curl -s -X POST localhost:8091/api/glossary \
  -H 'content-type: application/json' -d '{"text":"лукоморье|8, АйМоп"}'
docker compose exec stt-api cat /data/hotwords.txt
curl -s localhost:8091/api/status | grep -o '"status":"[a-z]*"'
```

Ожидается `{"count":2,"applied":true}`, файл с фразами через табуляцию и статус
`ready` — процесс движка не перезапускался, он перечитал глоссарий на месте.

Сразу после проверки вернуть список обратно и убедиться, что число фраз совпало с
тем, что показал `wc -l`:

```sh
python3 -c "import json; print(json.dumps({'text': open('hotwords.bak', encoding='utf-8').read()}, ensure_ascii=False))" > restore.json
curl -s -X POST localhost:8091/api/glossary -H 'content-type: application/json' -d @restore.json
```

## 5. Восстановление после падения движка

```sh
docker compose exec stt-api pkill -f 'gigastt serve'
sleep 5
curl -s localhost:8091/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d['restart_count'], d['detail'])"
curl -s -X POST localhost:8091/api/test -F file=@example.wav
```

Ожидается `ready 1 Готово: rnnt (после перезапуска)` в течение нескольких секунд
и рабочее распознавание после этого.

## 6. Восстановление после перезапуска контейнера

```sh
docker compose restart stt-api
sleep 10
curl -s localhost:8091/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d['engine']['variant'])"
```

Ожидается `ready rnnt` — конфигурация поднялась из `data/state.json`, веса
повторно не качались.

### 6.1. Расхождение с `.env` видно, а не молчит

Правило "состояние сильнее `.env`" остается, поэтому проверяется не то, что файл
победил, а то, что о разнице сказано вслух. Поменяйте в `.env` одну строку
(например `HOTWORDS_DEFAULT` на противоположное значение) и пересоздайте контейнер:

```sh
docker compose up -d
sleep 8
docker compose logs stt-api | grep ВНИМАНИЕ
curl -s localhost:8091/api/status | python3 -m json.tool | sed -n '/"env"/,/}/p'
```

Ожидается строка `ВНИМАНИЕ: .env расходится с сохраненной конфигурацией
(hotwords_default: .env=... / развернуто=...)` и непустое `env.diverges` в статусе.
В консоли это же видно блоком "в .env указано другое" в разделе "Настройки запуска".

Дальше применяется `.env` - в консоли кнопкой "Развернуть с настройками .env", в
терминале тем же вызовом, который делает кнопка:

```sh
curl -s -X POST localhost:8091/api/deploy -H 'content-type: application/json' \
  -d "$(curl -s localhost:8091/api/status | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["env"]["config"]))')"
sleep 20
ps -eo args | grep "[g]igastt serve"
curl -s localhost:8091/api/status | python3 -c "import json,sys; print(json.load(sys.stdin)['env']['diverges'])"
```

Ожидается, что флаг дошел до процесса (`--hotwords-default` есть или его нет,
следовательно как просит файл), а `env.diverges` стал пустым. Смотреть надо
`ps -eo args`, а не эндпоинт статуса: статус показывает желаемое, а не argv.

Фразы `INITIAL_CONTEXT`, которых нет в глоссарии, показываются там же, в разделе
"Глоссарий", вместе с кнопкой "Добавить из `.env`". Автоматически они не доливаются
намеренно: удаленная в консоли фраза не должна возвращаться после перезапуска.

## 7. Побочные модели лежат в томе

```sh
docker compose exec stt-api ls /models /models/punct
```

Ожидается голова (`v3_rnnt_*`), `punct/` с `rupunct_small_int8.onnx`, модель
диаризации и `optimized_cache`. Если `punct/` пуст, а пунктуация в тексте есть —
значит движок положил её в домашний каталог, и после пересоздания контейнера она
скачается заново: проверьте, что в `console/engine.py` передаются
`--punct-model-dir` и `--vad-model-dir`.

## 8. Защита ключом

```sh
smoke_key=$(openssl rand -hex 24) && printf '\nAPI_KEY=%s\n' "$smoke_key" >> .env
docker compose up -d --wait
curl -s -o /dev/null -w '%{http_code}\n' localhost:8091/v1/models                             # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $smoke_key" localhost:8091/v1/models # 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8091/health                                # 200
unset smoke_key
```

Удалите из тестового `.env` только что добавленную строку и выполните
`docker compose up -d --wait`, чтобы восстановить предыдущую настройку.

## 9. Уборка кеша графов

Штатная команда движка, безопасная с 2.18.0 (до неё она сносила граф работающей
головы — см. [решения](decisions.md)):

```sh
docker compose exec stt-api gigastt cache-gc --model-dir /models --dry-run
docker compose exec stt-api gigastt cache-gc --model-dir /models
```

`kept` считает графы для установленных весов, а не головы: на одну голову может
приходиться больше одного графа. `removed` относится к устаревшим
`*_optimized.ort` и `*_optimized.onnx`. Проверка, что работающий движок не пострадал:

```sh
docker compose exec stt-api grep -o '/models/optimized_cache/[^ ]*' /proc/*/maps
curl -s -X POST localhost:8091/api/test -F file=@example.wav
```

Замапленный файл должен остаться на месте, распознавание — работать.

## Уборка

```sh
docker compose down          # контейнер удалён, models/ и data/ остались
rm -f example.wav long_example.wav speech-16000.webm speech-48000.webm limit.wav hotwords.bak restore.json
```

Если проверка шла на рабочей установке, здесь же убедитесь, что глоссарий вернулся
(шаг 4) и что в `data/state.json` стоит та голова, которая была развёрнута до
проверки: сценарий разворачивает свою и оставляет её.
