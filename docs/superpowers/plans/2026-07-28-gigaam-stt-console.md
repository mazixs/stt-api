# GigaAM STT Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a one-command Docker service where a web page lets the user pick a GigaAM v3 head, press «Развернуть», and get an OpenAI-compatible STT API that survives crashes.

**Architecture:** One container, two processes. A Python/FastAPI "console" (port 8080) owns the UI, the OpenAI facade, the API key, the glossary, persisted state, and supervises `gigastt serve` as a child process bound to `127.0.0.1:9876`. All speech inference belongs to the upstream GigaSTT engine binary — the console never decodes audio itself.

**Tech Stack:** Python 3.13 (Debian trixie), FastAPI, uvicorn, httpx, pydantic-settings, pytest + pytest-asyncio, vanilla JS, Docker + Compose. Engine: `ghcr.io/ekhodzitsky/gigastt:2.15.0`.

## Global Constraints

- Engine image pinned to `ghcr.io/ekhodzitsky/gigastt:2.15.0`; binary lives at `/usr/local/bin/gigastt`.
- Console base image `debian:trixie-slim` — must match the engine image base so the engine binary's glibc/libstdc++ requirements are met.
- Console listens on `0.0.0.0:${CONSOLE_PORT:-8080}`. Engine listens on `127.0.0.1:9876` and is never published.
- Heads: `rnnt` (default), `e2e_rnnt`, `ml_ctc`, `ml_ctc_large`. No other model names are valid.
- Engine flags are the only way the console configures inference: `--model-dir`, `--model-variant`, `--punctuation`, `--itn`, `--vad`, `--pool-size`, `--hotwords-file`, `--hotwords-boost`, `--hotwords-default`, `--host`, `--port`, `--bind-all`.
- Model files per head (used for the "скачано" badge only; `gigastt download` stays authoritative):
  - `rnnt`: `v3_rnnt_encoder_int8.onnx`, `v3_rnnt_decoder.onnx`, `v3_rnnt_joint.onnx`, `v3_vocab.txt`
  - `e2e_rnnt`: `v3_e2e_rnnt_encoder_int8.onnx`, `v3_e2e_rnnt_decoder.onnx`, `v3_e2e_rnnt_joint.onnx`, `v3_e2e_rnnt_vocab.txt`
  - `ml_ctc`: `multilingual_ctc.int8.onnx`, `multilingual_vocab.txt`
  - `ml_ctc_large`: `multilingual_large_ctc.int8.onnx`, `multilingual_vocab.txt`
- Download progress contract: `gigastt download --progress json` emits one NDJSON object per line on stdout with `phase` ∈ `download|verify|quantize|done|error`. Exit codes: `0` ok, `65` checksum, `69` network, `74` disk, `130` interrupted, `1` other.
- Health contract: engine `GET /health` → `{"status":"ok","model":"gigaam-v3-rnnt","variant":"rnnt",...}`; during first-run download `model` is `"loading"`.
- Console `GET /health` returns 200 whenever the console process is alive, regardless of engine state (Docker healthcheck must not restart the container over an unloaded model).
- All user-visible strings (UI, README, error details) are in Russian. Code, comments, commit messages, log messages in English.
- Tests never start the real engine: `ENGINE_BIN` points at a fake CLI.
- Dev environment via `uv`: `uv sync`, `uv run pytest`.

---

### Task 1: Project skeleton and settings

**Files:**
- Create: `pyproject.toml`, `console/__init__.py`, `console/settings.py`, `.gitignore`
- Test: `tests/test_settings.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing
- Produces: `console.settings.Settings` (pydantic-settings `BaseSettings`) with fields
  `console_port: int = 8080`, `api_key: str = ""`, `model_variant: str = "rnnt"`,
  `punctuation: str = "auto"`, `itn: str = "auto"`, `vad: bool = False`,
  `pool_size: int = 1`, `initial_context: str = ""`, `hotwords_boost: float = 5.0`,
  `hotwords_default: bool = False`, `autostart: bool = True`, `max_upload_mb: int = 50`,
  `enable_jobs: bool = False`, `hf_token: str = ""`, `log_level: str = "info"`,
  `engine_bin: str = "/usr/local/bin/gigastt"`, `engine_host: str = "127.0.0.1"`,
  `engine_port: int = 9876`, `model_dir: Path = Path("/models")`, `data_dir: Path = Path("/data")`;
  properties `engine_base_url -> str`, `state_path -> Path`, `hotwords_path -> Path`;
  factory `get_settings() -> Settings` (module-level cache, `reset_settings_cache()` for tests).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings.py
from pathlib import Path
from console.settings import Settings

def test_defaults_are_dictation_friendly():
    s = Settings(_env_file=None)
    assert s.console_port == 8080
    assert s.model_variant == "rnnt"
    assert s.pool_size == 1          # all cores to one request => lowest latency
    assert s.autostart is True
    assert s.engine_base_url == "http://127.0.0.1:9876"

def test_env_overrides_and_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_VARIANT", "e2e_rnnt")
    monkeypatch.setenv("POOL_SIZE", "2")
    monkeypatch.setenv("VAD", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    assert s.model_variant == "e2e_rnnt"
    assert s.pool_size == 2
    assert s.vad is True
    assert s.state_path == tmp_path / "state.json"
    assert s.hotwords_path == tmp_path / "hotwords.txt"

def test_invalid_variant_rejected():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_variant="whisper-large")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'console'`

- [ ] **Step 3: Write minimal implementation**

`pyproject.toml` with `requires-python = ">=3.12"`, dependencies `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic-settings`, `python-multipart`; dev group `pytest`, `pytest-asyncio`, `anyio`. `[tool.pytest.ini_options] asyncio_mode = "auto"`.

`console/settings.py`: `Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`, a `Literal["rnnt","e2e_rnnt","ml_ctc","ml_ctc_large"]` for `model_variant`, `Literal["auto","on","off"]` for `punctuation`/`itn`, and the properties listed above.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock console/ tests/ .gitignore
git commit -m "feat: project skeleton and settings"
```

---

### Task 2: Head catalog and persisted state

**Files:**
- Create: `console/catalog.py`, `console/state.py`
- Test: `tests/test_catalog.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `console.settings.Settings`
- Produces:
  - `console.catalog.Head` dataclass: `id, title, subtitle, languages: list[str], native_punctuation: bool, size_mb: int, files: tuple[str, ...]`
  - `console.catalog.HEADS: dict[str, Head]` (insertion order = UI order: `rnnt`, `e2e_rnnt`, `ml_ctc`, `ml_ctc_large`)
  - `console.catalog.is_downloaded(head_id: str, model_dir: Path) -> bool`
  - `console.state.EngineConfig` frozen dataclass: `variant, punctuation, itn, vad, pool_size, hotwords_boost, hotwords_default`; `to_dict()`, `from_dict()`
  - `console.state.StateFile(path: Path)` with `load() -> State`, `save(State)`; `State` dataclass `status: str, detail: str, desired: EngineConfig | None, last_good: EngineConfig | None, updated_at: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
import json
from console.state import EngineConfig, State, StateFile

def cfg(**kw):
    base = dict(variant="rnnt", punctuation="auto", itn="auto", vad=False,
                pool_size=1, hotwords_boost=5.0, hotwords_default=False)
    return EngineConfig(**{**base, **kw})

def test_load_missing_file_returns_empty_state(tmp_path):
    st = StateFile(tmp_path / "state.json").load()
    assert st.status == "stopped"
    assert st.desired is None and st.last_good is None

def test_roundtrip_and_atomic_write(tmp_path):
    p = tmp_path / "state.json"
    sf = StateFile(p)
    sf.save(State(status="ready", detail="", desired=cfg(), last_good=cfg(variant="e2e_rnnt")))
    assert not list(tmp_path.glob("*.tmp"))            # no leftover temp files
    loaded = sf.load()
    assert loaded.status == "ready"
    assert loaded.desired.variant == "rnnt"
    assert loaded.last_good.variant == "e2e_rnnt"
    assert json.loads(p.read_text())["version"] == 1

def test_corrupt_file_degrades_to_empty_state(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert StateFile(p).load().status == "stopped"
```

```python
# tests/test_catalog.py
from console.catalog import HEADS, is_downloaded

def test_catalog_lists_four_heads_in_ui_order():
    assert list(HEADS) == ["rnnt", "e2e_rnnt", "ml_ctc", "ml_ctc_large"]
    assert HEADS["e2e_rnnt"].native_punctuation is True
    assert HEADS["rnnt"].native_punctuation is False
    assert "ru" in HEADS["rnnt"].languages
    assert "kk" in HEADS["ml_ctc"].languages

def test_is_downloaded_requires_every_file(tmp_path):
    assert is_downloaded("rnnt", tmp_path) is False
    for name in HEADS["rnnt"].files:
        (tmp_path / name).write_bytes(b"x")
    assert is_downloaded("rnnt", tmp_path) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py tests/test_catalog.py -v` → FAIL (modules missing)

- [ ] **Step 3: Write minimal implementation**

`catalog.py`: the four `Head` entries with the filenames from Global Constraints, Russian titles/subtitles (`rnnt` → «Точность максимум», subtitle «пунктуация отдельным проходом»; `e2e_rnnt` → «Пунктуация нативно»; `ml_ctc` / `ml_ctc_large` → «Мультиязычная, без пунктуации»). `is_downloaded` = all files exist and are non-empty.

`state.py`: dataclasses + `StateFile.save` writing to `path.with_suffix(".tmp")` then `os.replace`, creating parent dirs; `load` tolerating missing/corrupt JSON; `updated_at` via `datetime.now(UTC).isoformat()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py tests/test_catalog.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add console/catalog.py console/state.py tests/test_catalog.py tests/test_state.py
git commit -m "feat: head catalog and atomic state persistence"
```

---

### Task 3: Glossary → hotwords file

**Files:**
- Create: `console/glossary.py`
- Test: `tests/test_glossary.py`

**Interfaces:**
- Consumes: `Settings`
- Produces: `parse_context(raw: str) -> list[tuple[str, float | None]]`, `render_hotwords(entries) -> str`, `write_hotwords(path: Path, raw: str) -> int` (returns entry count, writes empty file when no entries), `read_glossary(path: Path) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary.py
from console.glossary import parse_context, render_hotwords, write_hotwords

def test_splits_on_commas_and_newlines_and_trims():
    assert parse_context(" АйМоп, GigaAM \n Кубернетес\n\n") == [
        ("АйМоп", None), ("GigaAM", None), ("Кубернетес", None)]

def test_keeps_multiword_phrases_intact():
    assert parse_context("Пётр Иванович Сидоров, ай ти отдел") == [
        ("Пётр Иванович Сидоров", None), ("ай ти отдел", None)]

def test_optional_weight_after_pipe():
    assert parse_context("АйМоп|8, GigaAM|2.5") == [("АйМоп", 8.0), ("GigaAM", 2.5)]

def test_ignores_broken_weight_but_keeps_phrase():
    assert parse_context("АйМоп|очень") == [("АйМоп", None)]

def test_deduplicates_case_insensitively_keeping_first():
    assert parse_context("GigaAM, gigaam, GIGAAM|3") == [("GigaAM", None)]

def test_renders_engine_format_tab_separated():
    assert render_hotwords([("АйМоп", 8.0), ("GigaAM", None)]) == "АйМоп\t8.0\nGigaAM\n"

def test_write_creates_file_and_counts(tmp_path):
    p = tmp_path / "hotwords.txt"
    assert write_hotwords(p, "АйМоп, GigaAM") == 2
    assert p.read_text() == "АйМоп\nGigaAM\n"
    assert write_hotwords(p, "   ") == 0
    assert p.read_text() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_glossary.py -v` → FAIL

- [ ] **Step 3: Write minimal implementation**

Split on `\n` and `,`, strip, drop empties, parse `phrase|weight` with `float()` in `try/except`, dedupe on `casefold()`, render `phrase` or `phrase\tweight`, always end with a trailing newline (empty string when no entries).

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/glossary.py tests/test_glossary.py
git commit -m "feat: glossary parsing into engine hotwords file"
```

---

### Task 4: Fake engine CLI (test double)

**Files:**
- Create: `tests/fake_gigastt.py` (executable), `tests/conftest.py` (extend)
- Test: `tests/test_fake_gigastt.py`

**Interfaces:**
- Consumes: nothing
- Produces: a CLI compatible with the two subcommands the console uses, driven by env vars:
  - `fake_gigastt.py download --model-dir D --model-variant V --progress json` → prints NDJSON `download`(×2) → `verify` → `quantize` → `done`, creates the head's files under `D`; `FAKE_DOWNLOAD_FAIL=network|checksum|disk` → prints `{"phase":"error",...}` and exits 69/65/74.
  - `fake_gigastt.py serve --port P --host H ...` → HTTP server: `GET /health` → `{"status":"ok","model":"gigaam-v3-<variant>","variant":"<variant>"}` (after `FAKE_STARTUP_DELAY` seconds, before which the port is closed), `POST /v1/audio/transcriptions` → `{"text":"привет мир"}` (or `text/plain`/SSE per `response_format`/`stream`), `POST /v1/transcribe` → engine-shaped JSON, `POST /v1/admin/reload` → `{"status":"ok"}` and bumps a counter exposed at `GET /debug/reloads`, `GET /debug/argv` → the argv it was started with; `FAKE_CRASH_AFTER=N` → `os._exit(1)` after N seconds; `FAKE_UNHEALTHY=1` → `/health` returns `{"status":"ok","model":"loading"}` forever.
  - pytest fixture `engine_bin` returning the path, and `console_settings(tmp_path)` building a `Settings` pointed at fakes and tmp dirs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fake_gigastt.py
import json, subprocess, sys

def test_download_emits_ndjson_and_creates_files(engine_bin, tmp_path):
    out = subprocess.run([sys.executable, engine_bin, "download", "--model-dir", str(tmp_path),
                          "--model-variant", "rnnt", "--progress", "json"],
                         capture_output=True, text=True, check=True).stdout
    phases = [json.loads(l)["phase"] for l in out.splitlines()]
    assert phases[0] == "download" and phases[-1] == "done"
    assert (tmp_path / "v3_rnnt_encoder_int8.onnx").exists()

def test_download_failure_exit_code(engine_bin, tmp_path, monkeypatch):
    env = {"FAKE_DOWNLOAD_FAIL": "network"}
    r = subprocess.run([sys.executable, engine_bin, "download", "--model-dir", str(tmp_path),
                        "--model-variant", "rnnt", "--progress", "json"],
                       capture_output=True, text=True, env={**dict(os.environ), **env})
    assert r.returncode == 69
    assert json.loads(r.stdout.splitlines()[-1])["kind"] == "network"
```

- [ ] **Step 2: Run test to verify it fails** → FAIL (fake missing)

- [ ] **Step 3: Write minimal implementation**

Plain `argparse` + `http.server.ThreadingHTTPServer` in `tests/fake_gigastt.py`; ignore unknown flags via `parse_known_args`.

- [ ] **Step 4: Run test to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_gigastt.py tests/conftest.py tests/test_fake_gigastt.py
git commit -m "test: fake gigastt CLI double"
```

---

### Task 5: Downloader

**Files:**
- Create: `console/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `Settings`, fake CLI
- Produces: `async download(settings, variant, on_event: Callable[[dict], None]) -> None`; raises `DownloadError(kind: str, message: str, exit_code: int)` where `kind` maps 65→`checksum`, 69→`network`, 74→`disk`, 130→`interrupted`, else `other`; forwards every NDJSON line to `on_event`, plus synthesizes `{"phase":"progress","percent":N}` events (0–100, integer, monotonic) computed from `bytes_done/bytes_total` summed across files.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_downloader.py
import pytest
from console.downloader import download, DownloadError

async def test_forwards_phases_and_percent(console_settings):
    seen = []
    await download(console_settings, "rnnt", seen.append)
    phases = [e["phase"] for e in seen]
    assert "download" in phases and phases[-1] == "done"
    percents = [e["percent"] for e in seen if e["phase"] == "progress"]
    assert percents == sorted(percents) and percents[-1] == 100

async def test_network_failure_raises_typed_error(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "network")
    with pytest.raises(DownloadError) as e:
        await download(console_settings, "rnnt", lambda ev: None)
    assert e.value.kind == "network" and e.value.exit_code == 69
    assert "сеть" in e.value.message.lower() or "сети" in e.value.message.lower()
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

`asyncio.create_subprocess_exec(sys.executable?, ...)` — build argv as `[engine_bin, "download", ...]` unless `engine_bin` ends with `.py`, in which case prefix `sys.executable`. Read stdout line by line, `json.loads` with `try/except` (ignore junk lines), aggregate per-file byte totals for the percent, drain stderr into the event stream as `{"phase":"log","line":...}`, map exit codes to Russian messages.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/downloader.py tests/test_downloader.py
git commit -m "feat: model download with NDJSON progress"
```

---

### Task 6: Engine process control

**Files:**
- Create: `console/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Settings`, `EngineConfig`, fake CLI
- Produces: `build_argv(settings, cfg) -> list[str]`; `class EngineProcess` with
  `async start(cfg)`, `async stop(timeout=15.0)`, `is_running -> bool`,
  `async wait_healthy(timeout=180.0) -> dict` (polls `GET /health` every 0.5 s until
  `status == "ok"` and `model != "loading"`; raises `EngineStartupError` on timeout or
  early exit), `async health() -> dict | None`, `async reload() -> bool`,
  `on_log: Callable[[str], None]`, `on_exit: Callable[[int], None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine.py
from console.engine import build_argv, EngineProcess, EngineStartupError
from console.state import EngineConfig

def test_argv_maps_config_to_engine_flags(console_settings):
    cfg = EngineConfig(variant="e2e_rnnt", punctuation="on", itn="off", vad=True,
                       pool_size=2, hotwords_boost=7.5, hotwords_default=True)
    argv = build_argv(console_settings, cfg)
    assert argv[1] == "serve"
    assert "--model-variant" in argv and argv[argv.index("--model-variant") + 1] == "e2e_rnnt"
    assert argv[argv.index("--punctuation") + 1] == "on"
    assert argv[argv.index("--itn") + 1] == "off"
    assert "--vad" in argv
    assert argv[argv.index("--pool-size") + 1] == "2"
    assert argv[argv.index("--hotwords-boost") + 1] == "7.5"
    assert "--hotwords-default" in argv
    assert "--bind-all" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--hotwords-file") + 1] == str(console_settings.hotwords_path)

def test_vad_and_hotwords_default_omitted_when_off(console_settings):
    cfg = EngineConfig("rnnt", "auto", "auto", False, 1, 5.0, False)
    argv = build_argv(console_settings, cfg)
    assert "--vad" not in argv and "--hotwords-default" not in argv

async def test_start_then_healthy_then_stop(console_settings):
    proc = EngineProcess(console_settings)
    await proc.start(EngineConfig("rnnt", "auto", "auto", False, 1, 5.0, False))
    health = await proc.wait_healthy(timeout=20)
    assert health["variant"] == "rnnt"
    await proc.stop()
    assert proc.is_running is False

async def test_wait_healthy_times_out_when_model_stays_loading(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_UNHEALTHY", "1")
    proc = EngineProcess(console_settings)
    await proc.start(EngineConfig("rnnt", "auto", "auto", False, 1, 5.0, False))
    with pytest.raises(EngineStartupError):
        await proc.wait_healthy(timeout=2)
    await proc.stop()

async def test_exit_callback_fires_on_crash(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "1")
    codes = []
    proc = EngineProcess(console_settings, on_exit=codes.append)
    await proc.start(EngineConfig("rnnt", "auto", "auto", False, 1, 5.0, False))
    await asyncio.sleep(3)
    assert codes and codes[0] != 0
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

`build_argv` per Global Constraints (always pass `--model-dir`, `--host`, `--port`, `--bind-all`, `--hotwords-file`, `--hotwords-boost`; conditional `--vad`, `--hotwords-default`; `--enable-jobs` when `settings.enable_jobs`). `EngineProcess.start` spawns with `stdout=stderr=PIPE`, launches a reader task feeding `on_log`, and a waiter task that fires `on_exit` for unexpected exits. `stop` sends SIGTERM, waits `timeout`, then SIGKILL. Env for the child: inherit + `RUST_LOG=gigastt=<log_level>`, `HF_TOKEN` when set.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/engine.py tests/test_engine.py
git commit -m "feat: engine child process control"
```

---

### Task 7: Event bus and latency metrics

**Files:**
- Create: `console/events.py`, `console/metrics.py`
- Test: `tests/test_events.py`, `tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `console.events.EventBus`: `publish(dict)`, `subscribe() -> AsyncIterator[dict]` (per-subscriber `asyncio.Queue(maxsize=100)`, oldest dropped when full), `log_lines(limit=200) -> list[str]`, `publish_log(line: str)` (also stored in a 500-line ring buffer)
  - `console.metrics.Metrics`: `record(audio_seconds: float | None, elapsed: float)`, `snapshot() -> dict` with `count`, `avg_elapsed`, `p95_elapsed`, `avg_rtf`, `last_elapsed` (rolling window of 20; `None` values when empty)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
from console.metrics import Metrics

def test_empty_snapshot():
    assert Metrics().snapshot() == {"count": 0, "avg_elapsed": None, "p95_elapsed": None,
                                    "avg_rtf": None, "last_elapsed": None}

def test_rolling_window_of_twenty_and_rtf():
    m = Metrics()
    for i in range(25):
        m.record(audio_seconds=10.0, elapsed=1.0)
    snap = m.snapshot()
    assert snap["count"] == 20 and snap["avg_rtf"] == 0.1 and snap["last_elapsed"] == 1.0

def test_rtf_skipped_when_duration_unknown():
    m = Metrics(); m.record(None, 2.0)
    assert m.snapshot()["avg_rtf"] is None and m.snapshot()["count"] == 1
```

```python
# tests/test_events.py
from console.events import EventBus

async def test_subscriber_receives_published_event():
    bus = EventBus()
    it = bus.subscribe()
    bus.publish({"type": "status", "status": "ready"})
    assert (await anext(it))["status"] == "ready"

async def test_slow_subscriber_drops_oldest_not_blocks():
    bus = EventBus(); it = bus.subscribe()
    for i in range(150):
        bus.publish({"type": "log", "i": i})
    first = await anext(it)
    assert first["i"] > 0                      # oldest dropped, no deadlock

def test_log_ring_buffer_keeps_last_lines():
    bus = EventBus()
    for i in range(600):
        bus.publish_log(f"line {i}")
    lines = bus.log_lines(limit=10)
    assert lines[-1] == "line 599" and len(lines) == 10
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

`EventBus` with `set[asyncio.Queue]`, `put_nowait` inside `try/except QueueFull` → `get_nowait()` then retry; `collections.deque(maxlen=500)` for logs. `Metrics` with `deque(maxlen=20)`; p95 via sorted index `int(0.95 * (n - 1))`.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/events.py console/metrics.py tests/test_events.py tests/test_metrics.py
git commit -m "feat: event bus and latency metrics"
```

---

### Task 8: Supervisor (deploy state machine, rollback, watchdog)

**Files:**
- Create: `console/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `Settings`, `StateFile`, `EngineConfig`, `EngineProcess`, `download`, `EventBus`, `glossary.write_hotwords`
- Produces: `class Supervisor(settings, bus)` with
  `status -> str` (`stopped|downloading|starting|ready|error`), `detail -> str`,
  `current -> EngineConfig | None`, `async deploy(cfg: EngineConfig) -> None`
  (serialized by an `asyncio.Lock`; writes hotwords file before start),
  `async stop_engine()`, `async apply_glossary(raw: str) -> bool` (rewrite file →
  `reload()`; on failure restart the process), `async restore_on_boot()`,
  `async shutdown()`, `backoff_delays = (1, 2, 4, 8, 16, 30)`.
  Every status change publishes `{"type": "status", ...}` on the bus and persists state.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor.py
async def test_deploy_downloads_starts_and_reaches_ready(console_settings, bus):
    sup = Supervisor(console_settings, bus)
    await sup.deploy(cfg())
    assert sup.status == "ready"
    assert (console_settings.model_dir / "v3_rnnt_encoder_int8.onnx").exists()
    assert StateFile(console_settings.state_path).load().last_good.variant == "rnnt"
    await sup.shutdown()

async def test_download_failure_keeps_previous_engine_running(console_settings, bus, monkeypatch):
    sup = Supervisor(console_settings, bus)
    await sup.deploy(cfg())                                    # rnnt is live
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "network")
    await sup.deploy(cfg(variant="e2e_rnnt"))
    assert sup.status == "error" and "сет" in sup.detail.lower()
    assert (await sup.health())["variant"] == "rnnt"            # old engine untouched
    await sup.shutdown()

async def test_failed_start_rolls_back_to_last_good(console_settings, bus, monkeypatch):
    sup = Supervisor(console_settings, bus)
    await sup.deploy(cfg())
    monkeypatch.setenv("FAKE_UNHEALTHY", "1")                   # new head never becomes ready
    await sup.deploy(cfg(variant="e2e_rnnt"))
    monkeypatch.delenv("FAKE_UNHEALTHY")
    assert sup.status == "ready" and sup.current.variant == "rnnt"
    assert "откат" in sup.detail.lower()
    await sup.shutdown()

async def test_watchdog_restarts_crashed_engine(console_settings, bus, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "1")
    sup = Supervisor(console_settings, bus)
    await sup.deploy(cfg())
    monkeypatch.delenv("FAKE_CRASH_AFTER")
    await wait_for(lambda: sup.restart_count >= 1, timeout=10)
    await wait_for(lambda: sup.status == "ready", timeout=30)
    await sup.shutdown()

async def test_restore_on_boot_redeploys_last_desired(console_settings, bus):
    StateFile(console_settings.state_path).save(
        State(status="ready", detail="", desired=cfg(variant="e2e_rnnt"), last_good=cfg(variant="e2e_rnnt")))
    sup = Supervisor(console_settings, bus)
    await sup.restore_on_boot()
    assert sup.status == "ready" and sup.current.variant == "e2e_rnnt"
    await sup.shutdown()

async def test_autostart_disabled_leaves_engine_stopped(console_settings, bus, monkeypatch):
    console_settings.autostart = False
    sup = Supervisor(console_settings, bus)
    await sup.restore_on_boot()
    assert sup.status == "stopped"

async def test_apply_glossary_reloads_without_restart(console_settings, bus):
    sup = Supervisor(console_settings, bus)
    await sup.deploy(cfg())
    pid = sup.process.pid
    assert await sup.apply_glossary("АйМоп, GigaAM") is True
    assert console_settings.hotwords_path.read_text() == "АйМоп\nGigaAM\n"
    assert await sup.debug_reload_count() == 1 and sup.process.pid == pid
    await sup.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

Deploy sequence exactly as the spec's state machine: persist desired + `downloading` → `download()` (errors → `error`, return without touching the running child) → write hotwords → `stop()` old → `start(cfg)` → `starting` → `wait_healthy()` → `ready` + `last_good = cfg`. On startup failure: publish error, then one rollback attempt to `last_good` (skip when equal or absent); rollback success sets `ready` with detail «откат на предыдущую модель». Watchdog: `on_exit` handler ignores exits during intentional stop/deploy (`self._expected_exit` flag), otherwise increments `restart_count` and schedules a restart task walking `backoff_delays`, capping at 30 s, resetting the index after a healthy start; after 5 consecutive failures status stays `error` while retries continue every 60 s.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/supervisor.py tests/test_supervisor.py
git commit -m "feat: supervisor with rollback and watchdog"
```

---

### Task 9: Auth and OpenAI facade

**Files:**
- Create: `console/auth.py`, `console/proxy.py`, `console/errors.py`
- Test: `tests/test_auth.py`, `tests/test_openai_facade.py`

**Interfaces:**
- Consumes: `Settings`, `Supervisor`, `Metrics`
- Produces:
  - `console.errors.openai_error(status: int, message: str, code: str, type_: str = "invalid_request_error") -> JSONResponse` producing `{"error": {"message","type","code"}}`
  - `console.auth.require_api_key(request) -> None` FastAPI dependency: no-op when `api_key` is empty, else 401 unless `Authorization: Bearer <key>` matches
  - `console.proxy.router` (`APIRouter`): `POST /v1/audio/transcriptions`, `POST /v1/audio/translations`, `GET /v1/models`, and a catch-all `{method} /v1/{path:path}` passthrough; records metrics on transcription calls

- [ ] **Step 1: Write the failing test**

```python
# tests/test_openai_facade.py
async def test_transcription_proxied_to_engine(client_ready):
    r = await client_ready.post("/v1/audio/transcriptions",
                                files={"file": ("a.wav", b"RIFFfake", "audio/wav")},
                                data={"model": "whisper-1"})
    assert r.status_code == 200 and r.json() == {"text": "привет мир"}

async def test_text_format_passes_through(client_ready):
    r = await client_ready.post("/v1/audio/transcriptions",
                                files={"file": ("a.wav", b"RIFFfake", "audio/wav")},
                                data={"response_format": "text"})
    assert r.headers["content-type"].startswith("text/plain") and r.text == "привет мир"

async def test_streaming_sse_passes_through(client_ready):
    async with client_ready.stream("POST", "/v1/audio/transcriptions",
                                   files={"file": ("a.wav", b"RIFFfake", "audio/wav")},
                                   data={"stream": "true"}) as r:
        body = "".join([chunk async for chunk in r.aiter_text()])
    assert "transcript.text.delta" in body and "[DONE]" in body

async def test_models_endpoint_lists_heads_and_alias(client_ready):
    data = (await client_ready.get("/v1/models")).json()
    ids = [m["id"] for m in data["data"]]
    assert data["object"] == "list"
    assert "gigaam-v3-rnnt" in ids and "whisper-1" in ids

async def test_translations_rejected_with_explanation(client_ready):
    r = await client_ready.post("/v1/audio/translations",
                                files={"file": ("a.wav", b"RIFFfake", "audio/wav")})
    assert r.status_code == 400
    assert "не переводит" in r.json()["error"]["message"]

async def test_503_with_retry_after_when_engine_not_ready(client_stopped):
    r = await client_stopped.post("/v1/audio/transcriptions",
                                  files={"file": ("a.wav", b"RIFFfake", "audio/wav")})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.json()["error"]["code"] == "engine_not_ready"

async def test_upload_larger_than_limit_rejected(client_ready):
    client_ready.app.state.settings.max_upload_mb = 1
    big = b"0" * (2 * 1024 * 1024)
    r = await client_ready.post("/v1/audio/transcriptions", files={"file": ("a.wav", big, "audio/wav")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"

async def test_metrics_recorded_for_successful_request(client_ready):
    await client_ready.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFFfake", "audio/wav")})
    assert client_ready.app.state.metrics.snapshot()["count"] == 1
```

```python
# tests/test_auth.py
async def test_open_when_no_key_configured(client_ready):
    assert (await client_ready.get("/v1/models")).status_code == 200

async def test_401_without_bearer_when_key_set(client_ready_with_key):
    r = await client_ready_with_key.get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"

async def test_200_with_correct_bearer(client_ready_with_key):
    r = await client_ready_with_key.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200

async def test_admin_api_also_protected(client_ready_with_key):
    assert (await client_ready_with_key.get("/api/status")).status_code == 401

async def test_health_never_protected(client_ready_with_key):
    assert (await client_ready_with_key.get("/health")).status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

Proxy reads the multipart body via `Request.form()` with the size check applied to the uploaded file (413 before forwarding), rebuilds it as `httpx` `files=`/`data=`, and streams the response back with `StreamingResponse` preserving `content-type` and status. Metrics: measure wall time; audio duration parsed from a WAV header when present (`struct` on `fmt`/`data` chunks), else `None`.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/auth.py console/proxy.py console/errors.py tests/test_auth.py tests/test_openai_facade.py
git commit -m "feat: OpenAI facade with api key auth"
```

---

### Task 10: Admin API and app assembly

**Files:**
- Create: `console/api.py`, `console/main.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: everything above
- Produces: `console.main.create_app(settings: Settings | None = None) -> FastAPI` wiring
  `app.state.settings/supervisor/bus/metrics`, lifespan that calls `restore_on_boot()`
  as a background task and `shutdown()` on exit, static mount at `/`, and
  `console.api.router` with `/api/status`, `/api/models`, `/api/deploy`, `/api/stop`,
  `/api/glossary` (GET/POST), `/api/events` (SSE), `/api/test`, plus unprotected `/health`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_api.py
async def test_status_shape(client_stopped):
    body = (await client_stopped.get("/api/status")).json()
    assert body["status"] == "stopped"
    assert body["engine"]["variant"] is None
    assert body["metrics"]["count"] == 0
    assert body["api_key_set"] is False
    assert body["glossary_count"] == 0

async def test_models_marks_downloaded(client_ready):
    heads = (await client_ready.get("/api/models")).json()["heads"]
    rnnt = next(h for h in heads if h["id"] == "rnnt")
    assert rnnt["downloaded"] is True and rnnt["native_punctuation"] is False
    assert len(heads) == 4

async def test_deploy_switches_head(client_ready):
    r = await client_ready.post("/api/deploy", json={"variant": "e2e_rnnt"})
    assert r.status_code == 202
    await wait_status(client_ready, "ready")
    assert (await client_ready.get("/api/status")).json()["engine"]["variant"] == "e2e_rnnt"

async def test_deploy_rejects_unknown_variant(client_ready):
    r = await client_ready.post("/api/deploy", json={"variant": "whisper-large"})
    assert r.status_code == 422

async def test_stop_then_status_stopped(client_ready):
    assert (await client_ready.post("/api/stop")).status_code == 200
    assert (await client_ready.get("/api/status")).json()["status"] == "stopped"

async def test_glossary_get_post_roundtrip(client_ready):
    r = await client_ready.post("/api/glossary", json={"text": "АйМоп, GigaAM"})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert (await client_ready.get("/api/glossary")).json()["text"] == "АйМоп\nGigaAM"

async def test_events_stream_emits_status_events(client_ready):
    async with client_ready.stream("GET", "/api/events") as r:
        await client_ready.post("/api/stop")
        chunk = await anext(r.aiter_text())
    assert "status" in chunk

async def test_test_endpoint_returns_text_and_timing(client_ready):
    r = await client_ready.post("/api/test", files={"file": ("a.wav", wav_bytes(seconds=1), "audio/wav")})
    body = r.json()
    assert body["text"] == "привет мир" and body["elapsed"] >= 0 and body["audio_seconds"] == 1.0

async def test_health_reports_engine_state_but_stays_200(client_stopped):
    r = await client_stopped.get("/health")
    assert r.status_code == 200 and r.json()["engine_status"] == "stopped"
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

Pydantic request models (`DeployRequest` with the `Literal` variant plus optional overrides defaulting to settings, `GlossaryRequest`). `/api/deploy` schedules `supervisor.deploy` via `asyncio.create_task` and returns 202. `/api/events` returns `StreamingResponse` of `data: {json}\n\n` from `bus.subscribe()`, prefixed with the current status snapshot and the last 50 log lines. `/api/test` reuses the proxy path and returns `{text, elapsed, audio_seconds, rtf}`.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/api.py console/main.py tests/test_admin_api.py
git commit -m "feat: admin api and app assembly"
```

---

### Task 11: Web UI

**Files:**
- Create: `console/static/index.html`, `console/static/app.js`, `console/static/style.css`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `/api/*` endpoints
- Produces: single-page UI with sections: статус, модели, проверка (файл + микрофон), глоссарий, подключение, логи

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ui.py
async def test_index_served_with_expected_sections(client_ready):
    html = (await client_ready.get("/")).text
    for marker in ["Статус", "Модели", "Проверка", "Глоссарий", "Подключение", "Логи"]:
        assert marker in html

async def test_app_js_only_calls_existing_endpoints(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    called = set(re.findall(r"fetch\(\s*[\"'`](/[^\"'`?]+)", js))
    routes = {r.path for r in client_ready.app.routes if hasattr(r, "path")}
    assert called and called <= routes | {"/api/events"}

async def test_mic_recording_produces_wav_client_side(client_ready):
    js = (await client_ready.get("/static/app.js")).text
    assert "AudioContext" in js and "RIFF" in js      # PCM->WAV in browser, no ffmpeg in image
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL

- [ ] **Step 3: Write minimal implementation**

`index.html` with the six sections and Russian copy; `app.js` polling `/api/status` every 2 s plus an `EventSource("/api/events")` subscription, deploy buttons per head, option toggles, drag-and-drop upload, mic capture via `getUserMedia` + `AudioContext` → 16 kHz mono PCM → WAV `Blob` built in JS, glossary textarea, curl/Python snippets filled from `location.origin`, log pane. `style.css` with light/dark via `prefers-color-scheme`.

- [ ] **Step 4: Run tests to verify they pass** → PASS

- [ ] **Step 5: Commit**

```bash
git add console/static tests/test_ui.py
git commit -m "feat: web console UI"
```

---

### Task 12: Docker, Compose, docs, and live verification

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `README.md`, `docs/manual-smoke.md`
- Modify: `README.md` (replace the placeholder)

**Interfaces:**
- Consumes: the whole console package
- Produces: `docker compose up -d` → console at `http://<host>:8080`

- [ ] **Step 1: Write the Dockerfile and compose file**

`Dockerfile`: `FROM ghcr.io/ekhodzitsky/gigastt:2.15.0 AS engine`, then `FROM debian:trixie-slim`, install `python3 python3-venv ca-certificates curl` (no ffmpeg, no torch), create venv at `/opt/venv`, `pip install` the pinned console deps, `COPY --from=engine /usr/local/bin/gigastt /usr/local/bin/gigastt`, copy `console/`, `ENV CONSOLE_PORT=8080 MODEL_DIR=/models DATA_DIR=/data`, `EXPOSE 8080`, `HEALTHCHECK curl -f http://localhost:8080/health`, `CMD ["/opt/venv/bin/python","-m","uvicorn","console.main:app","--host","0.0.0.0","--port","8080"]`.

`docker-compose.yml`: one service `stt-api`, `build: .`, `env_file: .env`, `init: true`, `restart: unless-stopped`, `ports: ["${CONSOLE_PORT:-8080}:8080"]`, volumes `./data:/data` and `./models:/models`, healthcheck, `mem_limit` note in comments.

- [ ] **Step 2: Build the image and verify the engine binary runs**

```bash
docker compose build
docker compose run --rm --entrypoint /usr/local/bin/gigastt stt-api --version
```
Expected: prints the gigastt version (proves the trixie base satisfies the binary).

- [ ] **Step 3: Run the stack and verify the real deploy path end to end**

```bash
cp .env.example .env
docker compose up -d
curl -s localhost:8080/health
curl -s -X POST localhost:8080/api/deploy -H 'content-type: application/json' -d '{"variant":"rnnt"}'
# poll until ready (downloads ~220 MB INT8 bundle on first run)
until curl -s localhost:8080/api/status | grep -q '"status":"ready"'; do sleep 5; done
```
Expected: status reaches `ready`; `docker compose logs` shows the download NDJSON progress then the engine boot.

- [ ] **Step 4: Verify OpenAI compatibility and measure real latency on this machine**

```bash
python3 - <<'PY'   # generate 5 s of speech-free WAV only to confirm plumbing
PY
curl -s -X POST localhost:8080/v1/audio/transcriptions -F file=@sample.wav -F model=whisper-1
curl -s localhost:8080/api/status | python3 -m json.tool | grep -A5 metrics
```
Expected: `{"text": ...}` and metrics showing measured `avg_rtf`. Record the observed numbers in the README.

- [ ] **Step 5: Verify crash recovery**

```bash
docker compose exec stt-api pkill -f 'gigastt serve'
sleep 8 && curl -s localhost:8080/api/status | grep '"status"'      # back to ready
docker compose restart stt-api
until curl -s localhost:8080/api/status | grep -q '"status":"ready"'; do sleep 5; done
```
Expected: watchdog restarts the engine after the kill; after a container restart the last head is redeployed automatically without re-downloading.

- [ ] **Step 6: Write README.md and docs/manual-smoke.md**

README (Russian): что это, быстрый старт в три команды, таблица переменных `.env`, как подключить OpenAI-клиент (curl + Python SDK), про глоссарий и почему `prompt` игнорируется, таблица голов с измеренными числами, раздел про восстановление после падений, откуда берутся модели и нужен ли `HF_TOKEN` (по факту проверки в шаге 3), лицензии (наш код MIT, движок MIT, веса GigaAM MIT).

- [ ] **Step 7: Full test suite and commit**

```bash
uv run pytest -q
git add Dockerfile docker-compose.yml .env.example .dockerignore README.md docs/manual-smoke.md
git commit -m "feat: docker packaging and documentation"
```

---

## Self-Review

**Spec coverage:** цель и сценарий — Task 12 (запуск одной командой) + Task 11 (UI); движок как зависимость — Global Constraints + Task 12; архитектура двух процессов — Tasks 6, 8, 10; компоненты — Tasks 1–11 по модулям один к одному; автомат развёртывания и авто-откат — Task 8; восстановление трёх уровней — Task 8 (watchdog, restore_on_boot) + Task 12 (шаг 5, `restart: unless-stopped`); API консоли — Task 10; OpenAI-фасад с 503/413/translations — Task 9; конфигурация `.env` — Task 1 + `.env.example` в Task 12; веб-интерфейс шесть секций — Task 11; Docker — Task 12; обработка ошибок — Tasks 5 (коды выхода), 8 (сбои деплоя), 9 (HTTP-ошибки); тесты с двумя заглушками — Task 4 + тесты в каждой задаче; ручной прогон — Task 12 шаг 6; фаза 2 (WebSocket-диктовка) сознательно вне плана.

**Placeholders:** нет TBD/TODO; каждый шаг с кодом содержит код или точную команду.

**Type consistency:** `EngineConfig` создаётся в Task 2 и используется с тем же порядком полей в Tasks 6, 8, 10; `Settings` поля из Task 1 используются в `build_argv` (Task 6) и `.env.example` (Task 12); `status` строки (`stopped|downloading|starting|ready|error`) одинаковы в Tasks 8, 10, 11; имена файлов моделей совпадают между Global Constraints, Task 2 и Task 4.
