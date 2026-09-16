# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org/): breaking changes bump the
major number, new capabilities the minor, fixes the patch.

This file records *what* shipped in each version. *Why* it was done that way lives in
[docs/decisions.md](docs/decisions.md) (in Russian) and is not duplicated here.

The version number lives in `console/__init__.py`; both `pyproject.toml` and the schema
at `/api/openapi.json` (`info.version`) read it from there.

## [Unreleased]

### Added

- The web console is bilingual. English is the default and Russian is switched on with
  the EN/RU control in the header; the choice is kept in the browser and survives a
  reload. Nothing is auto-detected from the browser language.
- `GET /api/models` accepts `?lang=en|ru` and localises the head descriptions, badges
  and notes, falling back to English for an unknown value.
- The status payload carries `detail_code` and `detail_params` alongside `detail`, so
  the console composes the sentence in the chosen language instead of translating a
  finished string.

### Changed

- `README.md` is now the English one and the Russian text moved to `README.ru.md`; the
  two are cross-linked. The documents under `docs/` stay Russian only.
- Static assets are fingerprinted (`?v=<hash>`), so a changed `i18n.js`, `app.js` or
  `style.css` is picked up without a hard reload.

### Notes

- `detail` keeps its Russian wording on purpose: it is also the log line.

## [1.4.0] - 2026-09-05

### Added

- Deployment progress is shown on the card you clicked: the bar, the percentage and the
  phase labels moved onto the head being deployed. The server now names the target in a
  separate `deploying` field, so on a rollback the bar sits on the head being rolled
  back to, not on the one that failed to start.
- The "Подключение" (Connecting) section now links to the control API schema
  (`/api/docs`) and includes a snippet for listing models (`GET /v1/models`).

### Changed

- The progress block in the page header was removed; button busy state is now derived
  from the status stream instead of the response to `POST /api/deploy`.
- Engine output is stripped of ANSI escape codes on its way into the console, and the
  child process is started with `NO_COLOR=1`, which the engine honours - so `docker logs`
  is plain text too.
- Engine settings in `.env.example` are commented out: the web interface is the intended
  way to choose them, while `.env` remains for headless deployment. The precedence of
  `data/state.json` over the file did not change - only the sample and the documentation
  did.

### Fixed

- Cards flickering and progress being lost after pressing "Развернуть" (Deploy).
- Button busy state clearing before deployment had actually finished.
- Errors and rollback verdicts not appearing on the card that was clicked.
- ANSI escape codes in the "Логи" (Logs) section and in `docker logs`, in both places
  where the console starts the engine: `serve` and the weights download.
- The misleading impression that `.env` and the web interface were in conflict.

## [1.3.0] - 2026-09-04

### Added

- The glossary boost and the built-in dictionary are now visible controls in the deploy
  form instead of hidden settings.
- A mismatch between `.env` and the deployed state is now reported: it is logged at
  startup, returned in `/api/status` as an `env` block, shown in plain words in
  "Настройки запуска" (Startup settings), and applied with the "Развернуть с настройками
  `.env`" button. `INITIAL_CONTEXT` phrases missing from the glossary are listed with an
  "Добавить из .env" (Add from .env) button. State still takes precedence over the file.
- Head badges in the console now come from our own measurement rather than external
  tables: Shmyrev's leaderboard has no GigaAM v3, and the multilingual HF track has no
  Russian. The README was rewritten around the same point: first what to pick, then how
  long to wait.
- The benchmark now lives in the repository. [`bench/`](bench/README.md) runs a folder
  of recordings through `/api/test`, deploys the configuration it needs, and computes
  the WER of the second pass relative to the first. No new dependencies.

### Changed

- The built-in brand dictionary is enabled by default, after an A/B test on 27 minutes
  of real dictation rather than on the upstream description: one difference across the
  whole set, and it was a correction
  ([measurement](docs/research/head-choice-and-wer.md)).
- `MAX_UPLOAD_MB` on our production install was lowered from 1000 to 500 because we
  measured a gigabyte. It passes on time and text (200 files, 1206.7 s, 355,015
  characters over 9 hours of speech) but strains memory: the container peaked at 2786.7
  MiB against a 2861 MiB ceiling. At 500 MiB it is 600.8 s and 1716.0 MiB. The default
  in code remains 150.
- The engine was updated to 2.20.0. WAV file uploads did not change by a single byte,
  and WebM/Opus moved closer to WAV thanks to the engine's new Opus decoder
  ([measurement](docs/research/head-choice-and-wer.md)).

### Fixed

- The deploy form sent five of the seven settings, so the glossary boost and the
  built-in dictionary were silently taken from the previous state. All seven are now
  sent.

### Measured

- End-to-end timing of long dictation: RTF stays between 0.031 and 0.040 on recordings
  from 9 seconds to 10 minutes. Time is linear because the engine walks 24-second
  windows strictly one after another.
- `VAD` was evaluated against a criterion fixed in advance and left off: it does save
  time, but the text diverges by 3% on meaningful words, and on short dictation it is
  slower as well.

## [1.2.0] - 2026-09-01

### Changed

- `MAX_UPLOAD_MB` now defaults to 150 instead of 50. The old limit rejected a 73 MiB
  live dictation - exactly the scenario the service exists for. 150 MB is roughly 105
  minutes of MP3 at 192 kbit/s; the console passes the limit on to the engine itself.

### Measured

- Tested at the limit rather than by eye: 147 MiB, 107 minutes of MP3 in one stream,
  `e2e_rnnt`, `POOL_SIZE=1` - 227 s of engine time (RTF 0.035), 70,734 characters, peak
  container memory 697 MiB against a 3 GB ceiling. This does not stack with the graph
  rebuild peak: while the engine is coming up the API answers `503` and does not read
  the body.
- Noted along the way: an MP3 concatenated with `cat` is read by the engine only up to
  the end of the first stream ([open questions](docs/open-questions.md)).

## [1.1.1] - 2026-09-01

### Fixed

- A file that fit `MAX_UPLOAD_MB` exactly is no longer rejected. The engine's body limit
  matched ours, while the body is larger than the file by the multipart wrapper, so the
  client got `400 Invalid multipart body` instead of a clear refusal. The console now
  sets the engine's limit itself, with a megabyte of headroom.
- `MAX_UPLOAD_MB` above 50 finally works. Previously the engine kept its own 50 MiB and
  rejected a 60 MB upload even though our own refusal message advised raising that
  variable.

## [1.1.0] - 2026-08-31

The engine was updated to 2.19.0. Our code did not change - what the user gets did.

### Changed

- Streaming almost caught up with file upload. The engine's stable prefix is enabled,
  and the divergence between streamed and regular text fell from 7.7% to zero on a short
  sample and from 24.2% to 9.7% on a long one
  ([measurement](docs/research/head-choice-and-wer.md)).
- File upload did not change by a single byte: 16 pairs out of 16 across two heads and
  seven variants of the same audio, with latency and memory within run-to-run spread.
- The reason for the update is dull: the h2 patch for RUSTSEC-2026-0258 in the engine's
  HTTP stack, and `symphonia` 0.6.0 to 0.6.1 in its audio decoder.
- Documentation: the engine version is no longer duplicated in the README and
  `.env.example` - the pinned value lives in `docker-compose.yml`. The manual check now
  verifies the text of a WebM/Opus result, not just the response code; that is how we
  found an engine bug where WebM with a header other than 48 kHz is recognized as
  silence ([open questions](docs/open-questions.md)).

## [1.0.0] - 2026-08-20

The first numbered version. The count does not start at zero because by this point the
service was already running in production with a closed feature set: from here on it is
fixes, not construction.

### Added

- OpenAI-compatible facade: `POST /v1/audio/transcriptions` with `response_format`
  `json`/`text`/`srt`/`vtt`/`verbose_json`, streaming mode, `GET /v1/models`, and
  passthrough for the engine's native endpoints. The upload body reaches the engine byte
  for byte and the engine's response is not rewritten.
- Web console: choice of one of four GigaAM v3 heads deployed by a button, latency and
  RTF measured on every request, microphone recording in the browser, and live logs and
  status over SSE.
- Phrase-based glossary: the list is edited as chips, applied without restarting the
  engine, and every phrase shows whether the selected head can write it.
- Watchdog and rollback: a crashed engine is restarted after 1, 2, 4, 8, 16, 30 s and,
  after five consecutive failures, once a minute; a head that fails to start is rolled
  back to the last working one.
- Single-switch authentication: an empty `API_KEY` leaves the service open, a non-empty
  one closes everything except `/health`, including the docs at `/api/docs`. Where a
  browser cannot send a header, the key is accepted as `?api_key=`.

### Notes

- The engine is pinned at 2.18.0, the image is 341 MB, and the container memory ceiling
  is 3 GB.
- Licensed under Apache 2.0; the repository history begins with a single commit made
  when the sources were opened.

[Unreleased]: https://github.com/mazixs/stt-api/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/mazixs/stt-api/releases/tag/v1.4.0
[1.3.0]: https://github.com/mazixs/stt-api/releases/tag/v1.3.0
[1.2.0]: https://github.com/mazixs/stt-api/releases/tag/v1.2.0
[1.1.1]: https://github.com/mazixs/stt-api/releases/tag/v1.1.1
[1.1.0]: https://github.com/mazixs/stt-api/releases/tag/v1.1.0
[1.0.0]: https://github.com/mazixs/stt-api/releases/tag/v1.0.0
