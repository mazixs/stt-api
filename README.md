# STT Console: local Russian speech recognition

Your own speech recognition server with an OpenAI-compatible API and a web console:
pick a GigaAM v3 head, press "Deploy", and the API is live. Built for Russian
dictation on your own machine - no cloud, no GPU.

*Read this in [Russian](README.ru.md). The documents under [`docs/`](docs/) are in
Russian only.*

[![License Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Docker](https://img.shields.io/badge/docker-compose-blue)](docker-compose.yml)

Inside are the [GigaAM v3](https://github.com/salute-developers/GigaAM) model by
SberDevices and the [gigastt](https://github.com/ekhodzitsky/gigastt) engine: a single
Rust binary on ONNX Runtime, INT8, CPU only. No PyTorch, no ffmpeg, no CUDA; the image
is about 340 MB. The engine version is pinned in `GIGASTT_TAG`, and the number itself
lives in [`docker-compose.yml`](docker-compose.yml) - it is not repeated here so the
two cannot drift apart.

![Web console](docs/console.png)

## Contents

- [Quick start](#quick-start)
- [Which head to pick](#which-head-to-pick)
- [Measured speed](#measured-speed)
- [Connecting a client](#connecting-a-client)
- [Glossary and the built-in brand dictionary](#glossary-and-the-built-in-brand-dictionary)
- [`.env` settings](#env-settings)
- [What happens when things fail](#what-happens-when-things-fail)
- [Where things live](#where-things-live)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Development](#development)
- [Licenses](#licenses)

## Quick start

```sh
git clone https://github.com/mazixs/stt-api.git && cd stt-api
cp .env.example .env          # optional: without .env the defaults apply
docker compose up -d
```

Open `http://<server-address>:8091`, press "Развернуть" (Deploy) on the
**GigaAM v3 RNN-T end-to-end** card and wait for the "готово" (ready) status. The
first run downloads ~230 MB of weights; afterwards they stay in `models/` and are not
downloaded again.

## Which head to pick

| Head | Languages | Punctuation | Our WER, FLEURS ru | 165 s of audio | Badge |
|---|---|---|--:|--:|---|
| `e2e_rnnt` | ru | built into the model | **4.32%** | 6.6 s | best for Russian |
| `ml_ctc_large` | ru, en, kk, ky, uz | separate pass, manual | 6.25% | 11.2 s | best multilingual |
| `rnnt` | ru | separate RuPunct + ITN pass | 6.56% | 6.3 s | |
| `ml_ctc` | ru, en, kk, ky, uz | separate pass, manual | not measured | **5.8 s** | |

WER is our own measurement from 11.08.2026 on 300 Russian FLEURS phrases; the timing
is one dictation file on a 16-core desktop CPU, best of three runs.

**Take `e2e_rnnt`.** It is more accurate than the rest and the only one able to write
what a glossary is full of: capitals, Latin script, digits and hyphens. The `rnnt`
vocabulary holds only 32 lowercase Cyrillic letters, so "OpenWhispr" cannot be
produced at all. Out of the 140 phrases in our production glossary, `e2e_rnnt` can
write all 140.

**Take `ml_ctc_large` if you need five languages.** It is twice as slow, takes 2.5 GiB
of memory on the first run, and writes just as bare as `rnnt`.

**None of this maps onto external leaderboards, and we checked:** Shmyrev's leaderboard
has no GigaAM v3 at all (the table has not been updated since 14.09.2025), and the
multilingual track of the HF Open ASR Leaderboard contains no Russian. That is why the
badges in the console come from our own measurement
([write-up, in Russian](docs/research/head-choice-and-wer.md)).

## Measured speed

Measured on a 16-core desktop CPU, head `e2e_rnnt`, `POOL_SIZE=1`, best of three runs,
04.09.2026:

| Recording | Response | Faster than speech |
|---|--:|--:|
| 9 s | 0.30 s | x31 |
| 36 s | 1.38 s | x26 |
| 71 s | 2.52 s | x28 |
| 2.8 min | 6.32 s | x26 |
| 5.5 min | 12.97 s | x25 |
| 10.3 min | 24.53 s | x25 |

On our eight-core production machine the same work costs twice as much: RTF around
0.05, so six minutes of dictation honestly cost about 19 seconds of waiting.

**Time grows linearly, and that is the design, not a defect.** Anything longer than 30
seconds is cut by the engine into 24-second windows with a 2-second overlap and
processed strictly one after another. The only way to speed that up would be
processing windows in parallel, which the engine does not do. We tested silence
skipping (`VAD=1`): on 3-10 minute recordings it saves 20-30% of the time but changes
3% of the words, and on short dictation it is actually slower - so it stays off
([measurement, in Russian](docs/research/head-choice-and-wer.md)).

The console's own overhead is 5 to 25 milliseconds, i.e. 0.1% on a ten-minute file.
Your server is probably slower than ours, so the console **measures latency itself** on
every request and shows it in the "Статус" (Status) section.

**Your client adds its own time on top of ours.** OpenWhispr does not stream to a
custom `base_url` and by default pushes the finished text through an LLM cleanup pass
([write-up, in Russian](docs/product.md)).

## Connecting a client

The API is OpenAI-compatible, so any client that accepts a `base_url` will work:

```sh
curl -X POST http://<server>:8091/v1/audio/transcriptions \
  -F model=whisper-1 \
  -F file=@recording.wav
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://<server>:8091/v1", api_key="not-needed")
with open("recording.wav", "rb") as audio:
    print(client.audio.transcriptions.create(model="whisper-1", file=audio).text)
```

Supported: `response_format` = `json`, `text`, `srt`, `vtt`, `verbose_json`;
`timestamp_granularities[]` = `word` / `segment`; `stream=true` (SSE); audio formats
WAV, MP3, M4A/AAC, OGG/Vorbis, OGG/Opus, FLAC and WebM/Opus. The `model` field may hold
anything - the head you deployed is the one that runs.

Not supported: `/v1/audio/translations` (GigaAM does not translate speech) and the
`prompt` field - context is set through the glossary.

Two caveats, both confirmed by measurement. If you produce WebM yourself, tell `ffmpeg`
the sample rate explicitly (`-ar 48000` before `-c:a libopus`): the engine reads it from
the container, and a file whose header says anything other than 48 kHz returns code 200
and empty text. And `stream=true` on an uploaded file yields almost no intermediate
text - the engine received all the audio at once; live text needs live audio, and for
that the engine's native `/v1/ws` is proxied through
([write-up, in Russian](docs/open-questions.md)).

### What is exposed

The key is all-or-nothing: an empty `API_KEY` leaves the service fully open, a
non-empty one closes everything except `/health`.

| Path | Behind the key | What it does |
|---|---|---|
| `POST /v1/audio/transcriptions` | yes | Recognition, OpenAI-compatible |
| `GET /v1/models` | yes | Four heads plus `whisper-1` for clients with a hardcoded name |
| `POST /v1/audio/translations` | yes | Always `400`: GigaAM does not translate speech |
| `GET/POST/DELETE /v1/...` | yes | Passthrough for the engine's remaining native endpoints |
| `GET /api/status`, `/api/models`, `/api/glossary` | yes | Data for the web console |
| `POST /api/deploy`, `/api/stop`, `/api/glossary` | yes | Deploy a head, stop the engine, apply the glossary |
| `POST /api/test` | yes | One-off recognition with timing and RTF |
| `GET /api/events` | yes | SSE: status, download progress, logs |
| `GET /api/docs`, `/api/openapi.json` | yes | Swagger UI and the schema of the control API |
| `GET /health` | no | Liveness of the console itself: otherwise Docker's healthcheck would restart a healthy container |

The key is sent as `Authorization: Bearer <key>`, and where a header cannot be sent, as
`?api_key=<key>`. There are exactly two such places, both in the browser: `EventSource`
on `/api/events` and following a link to `/api/docs`. The usual price of a key in a URL
applies: it stays in browser history and in proxy logs.

## Glossary and the built-in brand dictionary

Names, terms and titles that must be recognized correctly are entered directly in the
web console, or - for a headless deployment - through `INITIAL_CONTEXT` in `.env` (it
is commented out in the sample and has to be uncommented):

```
INITIAL_CONTEXT=АйМоп, GigaAM|8, Петр Иванович Сидоров
```

Separators are a comma or a newline, a phrase weight follows a vertical bar, and the
overall strength of the hint is `HOTWORDS_BOOST`. This is hotword biasing: the hint
works during decoding, not as a post-edit of finished text. Edits apply without
restarting the engine, which re-reads the list in place. Import, export and adding a
single phrase live in the console; take a copy from time to time, since the list lives
in the `data/` volume and has no second home.

**A phrase is dropped if the head cannot write it.** Case is not the issue - the engine
tries the phrase both as written and in lowercase - but a foreign alphabet is fatal:
`OpenWhispr` is unreachable for `rnnt`. The console strikes such phrases through, by
name.

**What not to expect from the glossary: exact spelling of English terms.** The rule, as
confirmed on a live recording: a hint finishes what was almost recognized and does not
invent what was not heard - it corrected `H20` into `H200`, but never turned
`standart Bird` into `Thunderbird` at any boost
([write-up, in Russian](docs/research/head-choice-and-wer.md)).

**`HOTWORDS_DEFAULT=1` is on by default.** This is the engine's built-in dictionary: 30
consumer brands in lowercase Cyrillic (эпл, айфон, яндекс, вайлдберриз). It does not
help with technical vocabulary - that is what the glossary above is for. Across 27
minutes of real dictation the dictionary changed one word out of 2654, and changed it
toward the correct spelling
([measurement, in Russian](docs/research/head-choice-and-wer.md)).

**`INITIAL_CONTEXT` is read once**, when the glossary file is created - after that,
edits in the console win. Phrases from `.env` that are missing from the list are shown
next to the glossary with an "Добавить из .env" (Add from .env) button. They are
deliberately not merged automatically: a phrase deleted in the console must not come
back after a restart.

## `.env` settings

The full list with explanations is in [`.env.example`](.env.example). The essentials:

Console settings: read when the container starts, not selectable in the console.

| Variable | Default | Meaning |
|---|---|---|
| `HOST_PORT` | `8091` | Port on the host |
| `API_KEY` | empty | If set, `Authorization: Bearer <key>` is required |
| `AUTOSTART` | `1` | Bring up the last model when the container starts |
| `MAX_UPLOAD_MB` | `150` | File size limit; the engine is given the same value, with headroom |
| `HF_TOKEN` | empty | Not needed: weights come from GitHub Releases |
| `LOG_LEVEL` | `info` | Log verbosity |

Engine settings: selected in the console; in `.env` they are commented out and are only
needed for a headless deployment.

| Variable | Default | Meaning |
|---|---|---|
| `INITIAL_CONTEXT` | empty | Glossary |
| `MODEL_VARIANT` | `rnnt` | Head |
| `PUNCTUATION` / `ITN` | `auto` | Punctuation and numbers as digits |
| `HOTWORDS_BOOST` | `5.0` | Strength of the glossary hint |
| `HOTWORDS_DEFAULT` | `1` | Built-in dictionary of 30 consumer brands |
| `VAD` | `0` | Silence skipping: saves time on long recordings, changes the text |
| `POOL_SIZE` | `1` | 1 = minimum latency, best for dictation |
| `FILE_WINDOW_CONCURRENCY` | `1` | Windows of a long file processed at once; only works with `POOL_SIZE` of 2 or more |

**The choice of head lives in the state, not in `.env`.** What is deployed is stored in
`data/state.json`, and after the first deployment that beats `.env`: otherwise the head
you picked in the console would revert to the file's value on every container restart.
The engine lines in `.env` are commented out and are only needed for a first, headless
start. Uncomment them and let them diverge from the console's choice, and the console
will report the mismatch and offer to apply `.env`.

The console will not stay quiet about it. The mismatch is logged at startup, shown in
plain words in the "Настройки запуска" (Startup settings) section, and applied by the
"Развернуть с настройками `.env`" (Deploy with `.env` settings) button. What is
actually running is verified with `ps -eo args | grep "gigastt serve"`, not with the
status endpoint: the status shows intent.

## What happens when things fail

| What failed | What the service does |
|---|---|
| The engine process | The console restarts it after 1, 2, 4, 8, 16, 30 s; after five failures in a row, once a minute |
| The whole container | Docker restarts it (`restart: unless-stopped`) and the configuration is read from `data/state.json` |
| A weights download broke off | Two retries, then a clear error and a button to try again. A working model is not stopped |
| A new head failed to start | Automatic rollback to the previous working one |

Requests that land exactly at the moment of a crash are lost - the client must retry.
While the engine is not ready, the API answers `503` with a `Retry-After` header.

The container has a 3 GB memory ceiling and log rotation. The ceiling is sized not by
the ~120 MiB of normal operation but by the heaviest moment - rebuilding the graph after
a head change (2538 MiB for `ml_ctc_large`). The reasoning is in the comments of
`docker-compose.yml` and in [docs/decisions.md](docs/decisions.md) (in Russian).

## Where things live

| Path | What |
|---|---|
| `models/` | Model weights: head (~230 MB), punctuation (~31 MB), diarization (~27 MB) |
| `models/optimized_cache/` | Optimized ONNX graphs, ~300 MB per head. Removed with the engine's `cache-gc` command |
| `data/state.json` | The last deployed configuration |
| `data/hotwords.txt` | The glossary in the engine's format |
| `data/metrics.json` | Console counters: files, seconds of audio, recognition time |

Only the console's port is exposed. The engine listens on `127.0.0.1:9876` inside the
container, so the only way in is through the console, which checks the key.

## Troubleshooting

**The download breaks off halfway.** The service retries twice on its own. If that does
not help, download the head's four weight files by hand from the
[engine release](https://github.com/ekhodzitsky/gigastt/releases) and put them into
`models/` - the console will see them and start without downloading.

**The "Записать с микрофона" (Record from microphone) button does nothing.** Browsers
grant microphone access only on `localhost` or over HTTPS: open the console through an
SSH tunnel (`ssh -L 8091:localhost:8091 server`). File recognition always works.

**Logs.** `docker compose logs -f`, which also carries the engine's output. The last
lines are visible in the console's "Логи" (Logs) section.

**The port is taken.** Change `HOST_PORT` in `.env` and run `docker compose up -d`.

**`400 Invalid multipart body` on a large file.** That was the behavior before 1.1.1.
The console now sets the engine's limit itself, with headroom, and refuses with a
message about the size ([write-up, in Russian](docs/decisions.md)).

## How it works

The console owns the engine process rather than sitting next to it: `gigastt serve` is
its child process on `127.0.0.1:9876` and is never published. There is no Rust build in
this project; the binary comes from the upstream image.

| File | Responsibility |
|---|---|
| `console/main.py` | Application assembly: `Settings` -> `Supervisor` -> FastAPI, three routers, and static files with a content fingerprint in their URLs |
| `console/supervisor.py` | State machine (`stopped` / `downloading` / `starting` / `ready` / `error`), rollback to the last working configuration, watchdog for a crashed process, `.env` mismatch |
| `console/engine.py` | Lifecycle of `gigastt serve`: the only place where engine arguments are assembled |
| `console/downloader.py` | Weight downloads through the engine's own command, with its progress and checksums |
| `console/api.py` | The console's control API under `/api`, including the `env` block in the status |
| `console/proxy.py` | The OpenAI facade: key, size limit, clear errors when not ready, timing. The upload body reaches the engine byte for byte |
| `console/auth.py` | `Authorization: Bearer` check, disabled when `API_KEY` is empty |
| `console/state.py` | `data/state.json`, written atomically: a container killed mid-write leaves no fragment behind |
| `console/catalog.py` | The four heads: names, sizes, weight files, and badges from our own measurement |
| `console/vocab.py` | Reads a head's vocabulary from disk and answers which glossary phrases the engine can write |
| `console/glossary.py` | Parses the phrase list into the engine's format (`data/hotwords.txt`) |
| `console/events.py` | The event bus behind SSE on `/api/events` |
| `console/metrics.py` | Counters in `data/metrics.json` |
| `console/wavinfo.py`, `console/webminfo.py` | Duration of WAV and WebM/Opus without decoding - only for RTF, the audio is untouched |
| `console/static/` | Frontend with no build step and no dependencies. Microphone recordings are assembled into WAV in the browser, which is why the image needs no ffmpeg |

## Development

```sh
uv sync          # environment
uv run pytest    # tests; the engine is stubbed out, nothing is downloaded
```

Run the service on your own machine through the overlay - that way the container will
not resurrect itself after a reboot and will not collide with production by name:

```sh
cp .env.local.example .env.local
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.local.yml down    # do not forget
```

Releases are tagged `vX.Y.Z`; what went into each is in [CHANGELOG.md](CHANGELOG.md).
The number lives in `console/__init__.py`, and both `pyproject.toml` and the schema at
`/api/openapi.json` read it from there.

To measure a new head or a new engine version, use [`bench/`](bench/README.md): two
`run` passes and one `compare` command give you the timing and the WER of the second
pass relative to the first.

The manual check against a live engine is [`docs/manual-smoke.md`](docs/manual-smoke.md).
The remaining documents live in [`docs/`](docs/) and are written in Russian:
[product scope](docs/product.md), [technical decisions](docs/decisions.md),
[head choice and measurements](docs/research/head-choice-and-wer.md),
[open questions](docs/open-questions.md).

## Licenses

The console's code is Apache 2.0 ([LICENSE](LICENSE)). The gigastt engine is MIT. The
GigaAM v3 weights are MIT.
