"""Persisted deployment state.

The console must come back to whatever the user last deployed, so the desired
configuration is written to disk on every transition. Writes are atomic: a
container killed mid-write must never find a half-written state file.
"""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUSES = ("stopped", "downloading", "starting", "ready", "error")


@dataclass(frozen=True)
class EngineConfig:
    variant: str = "rnnt"
    punctuation: str = "auto"
    itn: str = "auto"
    vad: bool = False
    pool_size: int = 1
    hotwords_boost: float = 5.0  # the engine's own default; see console.settings
    hotwords_default: bool = True  # measured, not assumed; see console.settings
    # Сколько окон длинного файла движок декодирует одновременно (движок 2.21.0 и
    # новее). 1 - последовательно, как было всегда. Толк появляется только при
    # `pool_size` от 2: движок берёт свободный слот пула без ожидания, а при пуле 1
    # свободного слота нет и файл всё равно идёт последовательно.
    file_window_concurrency: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "EngineConfig | None":
        if not raw:
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class State:
    status: str = "stopped"
    detail: str = ""
    desired: EngineConfig | None = None
    last_good: EngineConfig | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class StateFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        if not isinstance(raw, dict):
            return State()
        status = raw.get("status")
        return State(
            status=status if status in STATUSES else "stopped",
            detail=str(raw.get("detail") or ""),
            desired=EngineConfig.from_dict(raw.get("desired")),
            last_good=EngineConfig.from_dict(raw.get("last_good")),
            updated_at=str(raw.get("updated_at") or datetime.now(UTC).isoformat()),
        )

    def save(self, state: State) -> None:
        state.updated_at = datetime.now(UTC).isoformat()
        payload = {
            "version": 1,
            "status": state.status,
            "detail": state.detail,
            "desired": state.desired.to_dict() if state.desired else None,
            "last_good": state.last_good.to_dict() if state.last_good else None,
            "updated_at": state.updated_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
