"""Model download driven by the engine's own CLI.

`gigastt download --progress json` prints one NDJSON event per line, so the
console never has to know download URLs or checksums — it just relays progress to
the UI and turns exit codes into messages a non-programmer can act on.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from .catalog import HEADS
from .settings import Settings

EventSink = Callable[[dict[str, Any]], None]

_MESSAGES = {
    "checksum": (
        "Контрольная сумма скачанного файла не совпала — файл повреждён. "
        "Нажмите «Развернуть» ещё раз, битый файл будет перекачан."
    ),
    "network": (
        "Ошибка сети при скачивании модели. Проверьте доступ в интернет с сервера "
        "и попробуйте снова."
    ),
    "disk": (
        "Не удалось записать модель на диск. Проверьте свободное место и права "
        "на папку ./models."
    ),
    "interrupted": "Скачивание модели прервано.",
}

_EXIT_KINDS = {65: "checksum", 69: "network", 74: "disk", 130: "interrupted"}


class DownloadError(RuntimeError):
    def __init__(self, kind: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.exit_code = exit_code


def engine_command(settings: Settings, *args: str) -> list[str]:
    """Command line for the engine binary; `.py` doubles run through this interpreter."""
    if settings.engine_bin.endswith(".py"):
        return [sys.executable, settings.engine_bin, *args]
    return [settings.engine_bin, *args]


class _Progress:
    """Turns per-file byte counters into one monotonic 0..100 percentage."""

    def __init__(self, expected_files: int) -> None:
        self.expected_files = max(expected_files, 1)
        self.fractions: dict[str, float] = {}
        self.last = -1

    def update(self, name: str, done: int, total: int) -> int | None:
        fraction = 1.0 if total <= 0 else min(done / total, 1.0)
        self.fractions[name] = max(self.fractions.get(name, 0.0), fraction)
        overall = sum(self.fractions.values()) / self.expected_files
        percent = max(self.last, min(int(overall * 100), 100))
        if percent == self.last:
            return None
        self.last = percent
        return percent

    def finish(self) -> int | None:
        if self.last >= 100:
            return None
        self.last = 100
        return 100


async def download(settings: Settings, variant: str, on_event: EventSink) -> None:
    """Download the weights for `variant`, relaying progress events to `on_event`.

    Idempotent: the engine skips files that are already present and verified.
    """
    head = HEADS.get(variant)
    if head is None:
        raise ValueError(f"unknown head: {variant}")

    settings.model_dir.mkdir(parents=True, exist_ok=True)
    argv = engine_command(
        settings,
        "download",
        "--model-dir",
        str(settings.model_dir),
        "--model-variant",
        variant,
        "--progress",
        "json",
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DownloadError("other", f"Не удалось запустить движок: {exc}", 1) from exc

    progress = _Progress(len(head.files))
    reported_error: dict[str, Any] = {}

    async def pump_stderr() -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                on_event({"phase": "log", "line": line})

    async def pump_stdout() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                on_event({"phase": "log", "line": line})
                continue
            if not isinstance(event, dict):
                continue
            phase = event.get("phase")
            if phase == "error":
                reported_error.update(event)
            on_event(event)
            if phase == "download":
                percent = progress.update(
                    str(event.get("file") or ""),
                    int(event.get("bytes_done") or 0),
                    int(event.get("bytes_total") or 0),
                )
                if percent is not None:
                    on_event({"phase": "progress", "percent": percent})
            elif phase == "done":
                percent = progress.finish()
                if percent is not None:
                    on_event({"phase": "progress", "percent": percent})

    await asyncio.gather(pump_stdout(), pump_stderr())
    code = await process.wait()
    if code == 0:
        return

    kind = reported_error.get("kind") or _EXIT_KINDS.get(code, "other")
    message = _MESSAGES.get(kind) or f"Скачивание модели не удалось (код {code})."
    raise DownloadError(kind, message, code)
