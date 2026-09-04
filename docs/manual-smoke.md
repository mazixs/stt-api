# Ручная проверка на живом движке

Автотесты подменяют движок заглушкой и ничего не качают. Этот сценарий гоняет
настоящий gigastt с настоящими весами GigaAM v3. Занимает 10–15 минут, из них
большую часть — скачивание модели.

Все команды выполняются из корня проекта. Ниже — то, что было получено на
16-ядерном настольном CPU 29.07.2026; на другом железе цифры будут другими,
проверяйте порядок величин и поведение, а не точные значения.

**Сценарий рассчитан на свежую установку.** Он разворачивает голову, правит глоссарий
и убивает процесс движка, поэтому на машине с рабочим томом сначала сделайте копию
`data/` целиком, а после проверки верните ее на место.

## 1. Сборка и запуск

```sh
cp .env.example .env
docker compose up -d
docker compose ps                       # ожидается: Up (healthy)
curl -s localhost:8091/health
```

Ожидается `{"status":"ok","engine_status":"stopped",...}` — консоль жива, модель
не развёрнута. Проверьте, что бинарник движка совместим с базовым образом:

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
В веб-консоли на `http://localhost:8091` те же этапы видны в разделе «Статус»,
а строки движка — в «Логах».

Если скачивание обрывается (обрыв TLS на большом файле), сервис делает два
повтора сам. Ручной обход — раздел «Если что-то пошло не так» в README.

## 3. Распознавание

Официальные примеры GigaAM:

```sh
curl -sLO https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav       # 11.3 с
curl -sLO https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/long_example.wav  # 71.2 с

curl -s -X POST localhost:8091/v1/audio/transcriptions -F model=whisper-1 -F file=@example.wav
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

В потоке на файловой загрузке до события `transcript.text.done` доходят одна-две
дельты — так и должно быть: движок получил весь звук сразу. Смотреть надо на текст в
`done`, он должен совпадать с обычной загрузкой (на движке до 2.19.0 расходился, см.
[замер](research/head-choice-and-wer.md)).

Отдельно — браузерный WebM/Opus, на котором до движка 2.17.0 падал OpenWhispr:
60-мс пакеты и сам контейнер отвергались с 422. **Смотрите текст, а не код ответа:**
движок отдаёт 200 и пустую строку, если в контейнере объявлена частота не 48 кГц
(см. [открытые вопросы](open-questions.md)), поэтому проверка по `%{http_code}` такую
тишину пропускает. Речь берём из настоящей записи, а не из синуса:

```sh
ffmpeg -y -i example.wav -ar 48000 -c:a libopus -frame_duration 60 -f webm speech.webm

# через консоль: ожидается тот же текст, что и на example.wav
curl -s -X POST localhost:8091/api/test -F file=@speech.webm

# и напрямую в движок, минуя консоль
docker compose cp speech.webm stt-api:/tmp/speech.webm
docker compose exec stt-api curl -s -X POST 127.0.0.1:9876/v1/audio/transcriptions \
  -F file=@/tmp/speech.webm
```

Пустой `text` при коде 200 — это ошибка, а не тишина в записи: переложите тот же
поток Opus в OGG (`ffmpeg -i speech.webm -c:a copy speech.ogg`) и сравните.

Консоль такие загрузки не переписывает — она только читает их длину, чтобы посчитать
RTF (`console/webminfo.py`), потому что в ответе формата `json` длительности нет.

## 3.5. Лимит размера

Файл ровно в `MAX_UPLOAD_MB` должен распознаваться, а файл больше — получать `413` от
консоли с текстом про размер. Ответ `400 Invalid multipart body` означает, что движку
не передали предел тела (см. [решения](decisions.md)):

```sh
MB=${MAX_UPLOAD_MB:-150}
python3 -c "
import os, struct, pathlib
n = (int(os.environ['MB'])*1024*1024 - 44) // 2
d = bytes(n*2)
h = b'RIFF' + struct.pack('<I', 36+len(d)) + b'WAVEfmt ' + struct.pack('<IHHIIHH',16,1,1,16000,32000,2,16) + b'data' + struct.pack('<I', len(d))
pathlib.Path('limit.wav').write_bytes(h+d)" MB=$MB

curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8091/v1/audio/transcriptions \
  -F model=whisper-1 -F file=@limit.wav                      # ожидается 200

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
printf 'API_KEY=проверка\n' >> .env
docker compose up -d
curl -s -o /dev/null -w '%{http_code}\n' localhost:8091/v1/models                                    # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer проверка' localhost:8091/v1/models # 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8091/health                                       # 200
```

Не забудьте убрать `API_KEY` из `.env`, если он не нужен.

## 9. Уборка кеша графов

Штатная команда движка, безопасная с 2.18.0 (до неё она сносила граф работающей
головы — см. [решения](decisions.md)):

```sh
docker compose exec stt-api gigastt cache-gc --model-dir /models --dry-run
docker compose exec stt-api gigastt cache-gc --model-dir /models
```

Ожидается `kept N graph(s)` по числу установленных голов и `removed` только на
устаревших `*_optimized.onnx`. Проверка, что работающий движок не пострадал:

```sh
docker compose exec stt-api grep -o '/models/optimized_cache/[^ ]*' /proc/*/maps
curl -s -X POST localhost:8091/api/test -F file=@example.wav
```

Замапленный файл должен остаться на месте, распознавание — работать.

## Уборка

```sh
docker compose down          # контейнер удалён, models/ и data/ остались
rm -f example.wav long_example.wav speech.webm limit.wav hotwords.bak restore.json
```

Если проверка шла на рабочей установке, здесь же убедитесь, что глоссарий вернулся
(шаг 4) и что в `data/state.json` стоит та голова, которая была развёрнута до
проверки: сценарий разворачивает свою и оставляет её.
