"""Задержка: сколько прошло от «подали звук» до «получили текст».

Диктовка живёт задержкой, поэтому консоль меряет каждый запрос вместо того, чтобы
ссылаться на чужие бенчмарки. Меряется весь путь через консоль — приём загрузки,
работа движка, отдача ответа, — а не только вызов движка: пользователь ждёт всё
целиком.

Три разных ответа на три разных вопроса:
  * итоги за всё время (`data/metrics.json`) — сколько файлов прошло через сервис и
    какова средняя задержка вообще; переживают перезапуск контейнера;
  * окно последних запросов — какова задержка сейчас, чтобы заметить, что стало
    хуже;
  * последние файлы поимённо — что именно было медленным.
"""

import json
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

WINDOW = 20
RECENT = 8
# Перцентиль по трём замерам — это не перцентиль, а случайное число из трёх: индекс
# 0.95*(n-1) при малом n просто указывает на середину. Ниже этого порога честнее
# ничего не показывать, чем показывать цифру, которая выглядит как статистика.
MIN_FOR_PERCENTILE = 10


@dataclass(frozen=True)
class Sample:
    """Один прошедший запрос — то, что показывается в списке последних файлов."""

    name: str
    audio_seconds: float | None
    elapsed: float
    at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "audio_seconds": round(self.audio_seconds, 2) if self.audio_seconds else None,
            "elapsed": round(self.elapsed, 3),
            "rtf": round(self.elapsed / self.audio_seconds, 3)
            if self.audio_seconds
            else None,
            "at": self.at,
        }


class Metrics:
    def __init__(self, path: Path | None = None, window: int = WINDOW) -> None:
        self._path = path
        self._elapsed: deque[float] = deque(maxlen=window)
        self._rtf: deque[float] = deque(maxlen=window)
        self._recent: deque[Sample] = deque(maxlen=RECENT)
        self._total_files = 0
        self._total_audio = 0.0
        self._total_elapsed = 0.0
        self._load()

    def record(self, audio_seconds: float | None, elapsed: float, name: str = "") -> None:
        self._elapsed.append(elapsed)
        if audio_seconds and audio_seconds > 0:
            self._rtf.append(elapsed / audio_seconds)
            self._total_audio += audio_seconds
        self._recent.appendleft(
            Sample(name=name or "без имени", audio_seconds=audio_seconds, elapsed=elapsed, at=time.time())
        )
        self._total_files += 1
        self._total_elapsed += elapsed
        self._save()

    def snapshot(self) -> dict[str, object]:
        elapsed = list(self._elapsed)
        rtf = list(self._rtf)
        return {
            "count": len(elapsed),
            "avg_elapsed": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
            "p95_elapsed": _percentile(elapsed, 0.95),
            "avg_rtf": round(sum(rtf) / len(rtf), 3) if rtf else None,
            "last_elapsed": round(elapsed[-1], 3) if elapsed else None,
            # Итоги за всё время: переживают перезапуск, поэтому отвечают на вопрос
            # «сколько сервис вообще отработал», а не «сколько с последней загрузки».
            "total_files": self._total_files,
            "total_audio_seconds": round(self._total_audio, 1),
            "total_elapsed_seconds": round(self._total_elapsed, 1),
            "avg_elapsed_total": round(self._total_elapsed / self._total_files, 3)
            if self._total_files
            else None,
            "recent": [sample.to_dict() for sample in self._recent],
        }

    # ------------------------------------------------------------- сохранение

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        # Счётчики читаются по одному и с проверкой: подпорченный файл не должен
        # ронять консоль, максимум — потерять историю.
        self._total_files = _as_int(raw.get("total_files"))
        self._total_audio = _as_float(raw.get("total_audio_seconds"))
        self._total_elapsed = _as_float(raw.get("total_elapsed_seconds"))

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "version": 1,
            "total_files": self._total_files,
            "total_audio_seconds": round(self._total_audio, 1),
            "total_elapsed_seconds": round(self._total_elapsed, 1),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # Счётчик не стоит того, чтобы из-за него падал запрос.
            pass


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, int | float) and value >= 0 else 0.0


def _percentile(values: list[float], fraction: float) -> float | None:
    """Перцентиль по ближайшему рангу, или None, если замеров слишком мало."""
    if len(values) < MIN_FOR_PERCENTILE:
        return None
    ordered = sorted(values)
    return round(ordered[int(fraction * (len(ordered) - 1))], 3)
