"""In-process event bus for the UI's SSE stream, plus a log tail buffer.

Publishing must never block or fail because a browser tab stopped reading, so each
subscriber has a bounded queue and the oldest event is dropped when it overflows.
"""

import asyncio
import logging
from collections import deque
from typing import Any

QUEUE_SIZE = 100
LOG_BUFFER = 500


class Subscription:
    """Async iterator over bus events.

    Registration happens in `EventBus.subscribe`, not on first read: an async
    generator would only register when the consumer pulls, silently dropping every
    event published in between.
    """

    def __init__(self, bus: "EventBus", queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._bus = bus
        self._queue = queue

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict[str, Any]:
        return await self._queue.get()

    async def aclose(self) -> None:
        self._bus.unsubscribe(self._queue)


class EventBus:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._logs: deque[str] = deque(maxlen=LOG_BUFFER)
        # Mirroring to a logger keeps `docker compose logs` useful: without it the
        # engine's own output would only ever be visible in the browser.
        self._logger = logger

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._queues):
            while True:
                try:
                    queue.put_nowait(event)
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - queue drained meanwhile
                        break

    def publish_log(self, line: str) -> None:
        self._logs.append(line)
        if self._logger is not None:
            self._logger.info(line)
        self.publish({"type": "log", "line": line})

    def log_lines(self, limit: int = 200) -> list[str]:
        lines = list(self._logs)
        return lines[-limit:] if limit > 0 else []

    def subscribe(self) -> Subscription:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.add(queue)
        return Subscription(self, queue)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)
