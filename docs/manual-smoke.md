# Ручная проверка на живом движке

Автотесты подменяют движок заглушкой и ничего не качают. Этот сценарий гоняет
настоящий gigastt с настоящими весами GigaAM v3. Занимает 10–15 минут, из них
большую часть — скачивание модели.

Все команды выполняются из корня проекта. Ниже — то, что было получено на
16-ядерном настольном CPU 29.07.2026; на другом железе цифры будут другими,
проверяйте порядок величин и поведение, а не точные значения.

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
# gigastt 2.18.0
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

Отдельно — браузерный WebM/Opus, на котором до движка 2.17.0 падал OpenWhispr:
60-мс пакеты и сам контейнер отвергались с 422. Обе команды должны вернуть 200,
причём первая идёт напрямую в движок, минуя консоль:

```sh
ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -ac 2 -c:a libopus \
  -frame_duration 60 -f webm sine60.webm

docker compose cp sine60.webm stt-api:/tmp/sine60.webm
docker compose exec stt-api curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST 127.0.0.1:9876/v1/audio/transcriptions -F file=@/tmp/sine60.webm

curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8091/v1/audio/transcriptions \
  -F model=whisper-1 -F file=@sine60.webm
```

Консоль такие загрузки не переписывает — она только читает их длину, чтобы посчитать
RTF (`console/webminfo.py`), потому что в ответе формата `json` длительности нет.

## 4. Глоссарий без перезапуска

```sh
curl -s -X POST localhost:8091/api/glossary \
  -H 'content-type: application/json' -d '{"text":"лукоморье|8, АйМоп"}'
docker compose exec stt-api cat /data/hotwords.txt
curl -s localhost:8091/api/status | grep -o '"status":"[a-z]*"'
```

Ожидается `{"count":2,"applied":true}`, файл с фразами через табуляцию и статус
`ready` — процесс движка не перезапускался, он перечитал глоссарий на месте.

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
rm -f example.wav long_example.wav
```
