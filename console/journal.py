"""Ограниченная история структурных событий, отдельно от технических логов."""

from collections import deque
from datetime import UTC, datetime
from typing import TypedDict
from uuid import UUID, uuid4

from .catalog import HEADS


class JournalEvent(TypedDict):
    id: str
    at: str
    category: str
    level: str
    code: str
    params: dict[str, str | int]


# Для каждого кода разрешены только перечисленные параметры и UUID операции.
_EVENTS = {
    "deploy.downloading": ("deploy", "info", ("variant",)),
    "download.retry": ("deploy", "warning", ("variant", "kind", "attempt", "attempts")),
    "download.failed": ("deploy", "error", ("variant", "kind", "attempts", "running")),
    "deploy.starting": ("deploy", "info", ("variant",)),
    "start.failed": ("deploy", "error", ("variant",)),
    "rollback.starting": ("deploy", "warning", ("variant", "failed")),
    "rollback.done": ("deploy", "info", ("variant", "failed")),
    "rollback.failed": ("deploy", "error", ("variant", "failed")),
    "deploy.ready": ("engine", "info", ("variant",)),
    "idle.stopped": ("engine", "info", ("variant",)),
    "watchdog.exited": ("engine", "warning", ("variant",)),
    "watchdog.restarting": ("engine", "warning", ("variant", "attempt")),
    "watchdog.ready": ("engine", "info", ("variant", "attempt")),
    "watchdog.failed": ("engine", "error", ("variant", "attempt")),
}
_DOWNLOAD_KINDS = {"network", "checksum", "disk", "interrupted", "other"}


def _safe_value(name: str, value: object) -> str | int | None:
    if name in ("variant", "failed", "running"):
        return value if type(value) is str and value in HEADS else None
    if name == "kind":
        # Движок может вернуть неизвестный kind: не пропускаем произвольную строку.
        return value if type(value) is str and value in _DOWNLOAD_KINDS else "other"
    if name in ("attempt", "attempts"):
        return value if type(value) is int and value > 0 else None
    return None


class EventJournal:
    def __init__(self) -> None:
        self._events: deque[JournalEvent] = deque(maxlen=200)

    def record(self, code: str, operation_id: UUID, **params: object) -> None:
        category, level, allowed = _EVENTS[code]
        safe_params: dict[str, str | int] = {"operation_id": operation_id.hex}
        for name in allowed:
            if name in params and (value := _safe_value(name, params[name])) is not None:
                safe_params[name] = value
        self._events.appendleft(
            {
                "id": uuid4().hex,
                "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "category": category,
                "level": level,
                "code": code,
                "params": safe_params,
            }
        )

    def snapshot(self) -> list[JournalEvent]:
        return [{**event, "params": event["params"].copy()} for event in self._events]
