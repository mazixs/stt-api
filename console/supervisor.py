"""Deployment state machine and crash watchdog.

Owns the answers to three questions the user cares about:
  * what is the engine doing right now (status + detail, streamed to the UI),
  * what should it be doing after a restart (persisted desired config),
  * what happens when something breaks (rollback to the last good config, and
    automatic restarts with backoff when the engine dies on its own).
"""

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

from .downloader import DownloadError, download
from .engine import EngineProcess, EngineStartupError, wait_uninterrupted
from .errors import ApiError
from .events import EventBus
from .glossary import parse_context, read_glossary, write_hotwords
from .journal import EventJournal
from .settings import Settings
from .state import EngineConfig, State, StateFile

STARTUP_TIMEOUT = 180.0
# A 225 MB download over a home connection deserves a couple of retries before we
# bother the user: transient resets are far more common than real outages.
DOWNLOAD_RETRY_DELAYS = (3.0, 10.0)
BACKOFF_DELAYS = (1, 2, 4, 8, 16, 30)
MAX_CONSECUTIVE_FAILURES = 5
FAILED_RETRY_DELAY = 60.0


def _fold(phrase: str) -> str:
    """Как движок сравнивает фразы глоссария: без регистра и с единой "е"."""
    return phrase.casefold().replace("ё", "е")


class Supervisor:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self.journal = EventJournal()
        self.state_file = StateFile(settings.state_path)
        stored = self.state_file.load()

        self._status = "stopped"
        self._detail = ""
        # Рядом с готовой русской строкой едет код и его подстановки: интерфейс
        # двуязычный, а переводить обратно уже собранное предложение нельзя.
        self._detail_code: str | None = None
        self._detail_params: dict[str, Any] = {}
        self._desired: EngineConfig | None = stored.desired
        self._last_good: EngineConfig | None = stored.last_good
        self._download_percent: int | None = None
        # Цель развертывания, а не работающий процесс: пока веса качаются, работающего
        # процесса может не быть вовсе, а карточке в консоли уже есть что показать.
        self._deploying: str | None = None

        self.process = EngineProcess(
            settings,
            on_log=self.bus.publish_log,
            on_exit=self._on_engine_exit,
        )
        self.restart_count = 0
        self._start_error = ""
        self._lock = asyncio.Lock()
        self._comparison_task: asyncio.Task[Any] | None = None
        self._native_reload_task: asyncio.Task[Any] | None = None
        self._glossary_task: asyncio.Task[bool] | None = None
        self._shutting_down = False
        self._consecutive_failures = 0
        self._backoff_index = 0
        self._restart_task: asyncio.Task[None] | None = None
        self._recovery_operation: UUID | None = None

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> str:
        return self._status

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def detail_code(self) -> str | None:
        """Что именно происходит, машиночитаемо: консоль пишет это словами языка."""
        return self._detail_code

    @property
    def detail_params(self) -> dict[str, Any]:
        return dict(self._detail_params)

    @property
    def current(self) -> EngineConfig | None:
        """Configuration the engine is actually running."""
        return self.process.config if self.process.is_running else None

    @property
    def desired(self) -> EngineConfig | None:
        return self._desired

    @property
    def download_percent(self) -> int | None:
        return self._download_percent

    @property
    def deploying(self) -> str | None:
        """Голова, которую разворачиваем сейчас; None, когда ничего не происходит.

        Отличается от `current`: там процесс, который уже работает, а здесь цель. При
        откате целью становится прежняя голова - полоса прогресса должна переехать на
        ее карточку, а не остаться на той, что не поднялась.
        """
        return self._deploying if self._status in ("downloading", "starting") else None

    @property
    def glossary_count(self) -> int:
        return len(parse_context(read_glossary(self.settings.hotwords_path)))

    def default_config(self) -> EngineConfig:
        """Config preselected in the UI: last deployment, else the .env values."""
        return self._desired or self._last_good or EngineConfig(
            variant=self.settings.model_variant,
            punctuation=self.settings.punctuation,
            itn=self.settings.itn,
            vad=self.settings.vad,
            pool_size=self.settings.pool_size,
            hotwords_boost=self.settings.hotwords_boost,
            hotwords_default=self.settings.hotwords_default,
        )

    def env_config(self) -> EngineConfig:
        """То, что просит `.env`, без оглядки на сохраненное состояние."""
        return self.settings.engine_config_from_env()

    def env_divergence(self) -> dict[str, dict[str, Any]]:
        """Явно заданные в .env значения, которые расходятся с тем, что развернется.

        Состояние сильнее `.env` намеренно: иначе выбранная в консоли голова
        откатывалась бы к значению из файла после каждого перезапуска контейнера, а это
        худшая из гонок. Поэтому расхождение не исправляется само - оно показывается, а
        исправляет его пользователь одной кнопкой.
        """
        env = self.env_config().to_dict()
        state = self.default_config().to_dict()
        return {
            name: {"env": env[name], "state": state[name]}
            for name in sorted(self.settings.explicit_engine_fields())
            if env[name] != state[name]
        }

    def initial_context_missing(self) -> list[str]:
        """Фразы INITIAL_CONTEXT, которых нет в глоссарии, - сравнение как у движка.

        Автоматического слияния нет намеренно: фраза, удаленная в интерфейсе, не должна
        возвращаться после перезапуска только потому, что осталась в `.env`.
        """
        present = {
            _fold(phrase)
            for phrase, _weight in parse_context(read_glossary(self.settings.hotwords_path))
        }
        return [
            phrase
            for phrase, _weight in parse_context(self.settings.initial_context)
            if _fold(phrase) not in present
        ]

    async def health(self) -> dict[str, Any] | None:
        return await self.process.health()

    def _set_status(
        self,
        status: str,
        detail: str = "",
        code: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self._status = status
        self._detail = detail
        self._detail_code = code
        self._detail_params = params or {}
        if status not in ("downloading", "starting"):
            self._deploying = None
        self.state_file.save(
            State(
                status=status,
                detail=detail,
                desired=self._desired,
                last_good=self._last_good,
            )
        )
        self.bus.publish(
            {
                "type": "status",
                "status": status,
                "detail": detail,
                "detail_code": code,
                "detail_params": self._detail_params,
                "variant": self._deploying or (self.current.variant if self.current else None),
                "percent": self._download_percent,
            }
        )

    # ------------------------------------------------------------------ compare gate

    @property
    def comparison_allowed(self) -> bool:
        recovering = self._restart_task is not None and not self._restart_task.done()
        return not (self._shutting_down or self._lock.locked() or recovering)

    def begin_comparison[T](
        self, run: Callable[[], Coroutine[Any, Any, T]],
    ) -> asyncio.Task[T] | None:
        # Без await: проверка и регистрация неделимы относительно deploy/watchdog.
        if not self.comparison_allowed or self._comparison_task is not None:
            return None
        task = asyncio.create_task(run())
        self._comparison_task = task
        task.add_done_callback(self._clear_comparison)
        return task

    def _clear_comparison(self, task: asyncio.Task[Any]) -> None:
        if self._comparison_task is task:
            self._comparison_task = None

    async def _cancel_comparison(self) -> None:
        if (task := self._comparison_task) is not None and not task.done():
            task.cancel()
            # Одной отмены мало: сначала дождаться kill/reap и удаления файлов.
            await asyncio.gather(task, return_exceptions=True)

    async def run_native_reload[T](self, run: Callable[[], Coroutine[Any, Any, T]]) -> T | None:
        if (active := self._native_reload_task) is not None and not active.done():
            return None

        async def locked() -> T:
            async with self._lock:
                if self._shutting_down:
                    raise asyncio.CancelledError
                await self._cancel_comparison()
                try:
                    return await run()
                except asyncio.CancelledError:
                    # Отмена HTTP не останавливает сборку: сначала завершить сам движок.
                    if not self._shutting_down:
                        await wait_uninterrupted(asyncio.create_task(self.process.stop()))
                    raise

        # Закрытие клиентского соединения не останавливает сборку ONNX в upstream.
        task = asyncio.create_task(locked())
        self._native_reload_task = task
        task.add_done_callback(self._native_reload_done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise ApiError(503, "Перезагрузка прервана остановкой движка.", "engine_not_ready", "server_error") from None
            raise

    def _native_reload_done(self, task: asyncio.Task[Any]) -> None:
        if self._native_reload_task is task:
            self._native_reload_task = None
        if not task.cancelled():
            task.exception()  # У клиента, который ушёл, больше нет await этой задачи.

    # ------------------------------------------------------------------ deploy

    async def deploy(self, cfg: EngineConfig, startup_timeout: float = STARTUP_TIMEOUT) -> None:
        async with self._lock:
            await self._cancel_comparison()
            await self._deploy_locked(cfg, startup_timeout)

    async def _deploy_locked(self, cfg: EngineConfig, startup_timeout: float) -> None:
        operation_id = uuid4()
        self._desired = cfg
        self._deploying = cfg.variant
        self._download_percent = None
        self._set_status(
            "downloading",
            f"Проверяю и скачиваю модель ({cfg.variant})",
            "deploy.downloading",
            {"variant": cfg.variant},
        )
        self.journal.record("deploy.downloading", operation_id, variant=cfg.variant)
        if not await self._download_with_retries(cfg, operation_id):
            return
        self._download_percent = None

        self._seed_hotwords()
        await self.process.stop()
        self._set_status(
            "starting",
            f"Запускаю движок ({cfg.variant})",
            "deploy.starting",
            {"variant": cfg.variant},
        )
        self.journal.record("deploy.starting", operation_id, variant=cfg.variant)
        started = await self._start_and_wait(cfg, startup_timeout)
        if started:
            self._last_good = cfg
            self._consecutive_failures = 0
            self._backoff_index = 0
            self._set_status(
                "ready", f"Готово: {cfg.variant}", "deploy.ready", {"variant": cfg.variant}
            )
            self.journal.record("deploy.ready", operation_id, variant=cfg.variant)
            return
        self.journal.record("start.failed", operation_id, variant=cfg.variant)
        await self._rollback(cfg, startup_timeout, operation_id)

    async def _download_with_retries(self, cfg: EngineConfig, operation_id: UUID) -> bool:
        attempts = len(DOWNLOAD_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                await download(self.settings, cfg.variant, self._on_download_event)
                return True
            except DownloadError as exc:
                self._download_percent = None
                retriable = exc.kind in ("network", "checksum") and attempt < attempts
                if retriable:
                    delay = DOWNLOAD_RETRY_DELAYS[attempt - 1]
                    self._set_status(
                        "downloading",
                        f"{exc.message} Пробую снова ({attempt + 1} из {attempts})…",
                        "download.retry",
                        {"kind": exc.kind, "attempt": attempt + 1, "attempts": attempts},
                    )
                    self.journal.record(
                        "download.retry",
                        operation_id,
                        variant=cfg.variant,
                        kind=exc.kind,
                        attempt=attempt + 1,
                        attempts=attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                running = self.current
                detail = exc.message
                if attempt > 1:
                    detail += f" Попыток было {attempt}."
                if running is not None:
                    detail += f" Продолжает работать прежняя модель ({running.variant})."
                self._set_status(
                    "error",
                    detail,
                    "download.failed",
                    {
                        "kind": exc.kind,
                        "attempts": attempt,
                        "running": running.variant if running else None,
                    },
                )
                self.journal.record(
                    "download.failed",
                    operation_id,
                    variant=cfg.variant,
                    kind=exc.kind,
                    attempts=attempt,
                    running=running.variant if running else None,
                )
                return False
        return False

    async def _start_and_wait(self, cfg: EngineConfig, timeout: float) -> bool:
        await self._cancel_comparison()
        try:
            await self.process.start(cfg)
            if self._shutting_down:
                await self.process.stop()
                return False
            await self.process.wait_healthy(timeout=timeout)
            if not self.process.is_running:
                raise EngineStartupError("Движок завершился сразу после проверки готовности.")
            return True
        except (EngineStartupError, OSError, RuntimeError) as exc:
            self.bus.publish_log(f"engine failed to start: {exc}")
            self._start_error = str(exc)
            await self.process.stop()
            return False

    async def _rollback(
        self, failed: EngineConfig, startup_timeout: float, operation_id: UUID
    ) -> None:
        reason = self._start_error or "движок не запустился"
        target = self._last_good
        if target is None or target == failed:
            self._set_status(
                "error",
                f"Не удалось запустить модель {failed.variant}: {reason}",
                "start.failed",
                {"variant": failed.variant, "reason": reason},
            )
            return
        self._deploying = target.variant
        self._set_status(
            "starting",
            f"Откат на предыдущую модель ({target.variant})",
            "rollback.starting",
            {"variant": target.variant},
        )
        self.journal.record(
            "rollback.starting", operation_id, variant=target.variant, failed=failed.variant
        )
        if await self._start_and_wait(target, startup_timeout):
            self._desired = target
            self._set_status(
                "ready",
                f"Откат на предыдущую модель ({target.variant}): "
                f"модель {failed.variant} не запустилась — {reason}",
                "rollback.done",
                {"variant": target.variant, "failed": failed.variant, "reason": reason},
            )
            self.journal.record(
                "rollback.done", operation_id, variant=target.variant, failed=failed.variant
            )
            return
        self._set_status(
            "error",
            f"Модель {failed.variant} не запустилась ({reason}), откат на "
            f"{target.variant} тоже не удался.",
            "rollback.failed",
            {"variant": target.variant, "failed": failed.variant, "reason": reason},
        )
        self.journal.record(
            "rollback.failed", operation_id, variant=target.variant, failed=failed.variant
        )

    def _on_download_event(self, event: dict[str, Any]) -> None:
        phase = event.get("phase")
        if phase == "progress":
            self._download_percent = int(event.get("percent") or 0)
            self.bus.publish(
                {
                    "type": "download",
                    "percent": self._download_percent,
                    "variant": self._deploying,
                }
            )
        elif phase == "log":
            self.bus.publish_log(str(event.get("line") or ""))
        elif phase in ("verify", "quantize"):
            human = "проверяю контрольную сумму" if phase == "verify" else "квантую в INT8"
            self.bus.publish_log(f"{human}: {event.get('file') or ''}")
        elif phase == "error":
            self.bus.publish_log(f"download error: {event.get('message') or ''}")

    def _seed_hotwords(self) -> None:
        """Create the hotwords file from INITIAL_CONTEXT once; UI edits win afterwards."""
        if not self.settings.hotwords_path.exists():
            count = write_hotwords(self.settings.hotwords_path, self.settings.initial_context)
            if count:
                self.bus.publish_log(f"glossary seeded from INITIAL_CONTEXT: {count} phrases")

    # ------------------------------------------------------------------ glossary

    async def apply_glossary(self, raw: str) -> bool:
        async with self._lock:
            if self._shutting_down:
                return False
            self._glossary_task = asyncio.create_task(self._apply_glossary_locked(raw))
            try:
                return await wait_uninterrupted(self._glossary_task)
            finally:
                self._glossary_task = None

    async def _apply_glossary_locked(self, raw: str) -> bool:
        count = write_hotwords(self.settings.hotwords_path, raw)
        self.bus.publish_log(f"glossary updated: {count} phrases")
        if not self.process.is_running:
            return True
        # Reload тоже создаёт новый пул ONNX-сессий до подмены старого.
        await self._cancel_comparison()
        if await self.process.reload():
            self.bus.publish({"type": "glossary", "count": count})
            return True
        if self._shutting_down:
            return False
        self.bus.publish_log("engine reload failed, restarting the engine instead")
        cfg = self.process.config or self.default_config()
        await self.process.stop()
        if self._shutting_down:
            return False
        self._deploying = cfg.variant
        self._set_status(
            "starting", "Перезапускаю движок для нового глоссария", "glossary.restarting"
        )
        if await self._start_and_wait(cfg, STARTUP_TIMEOUT):
            self._set_status(
                "ready", f"Готово: {cfg.variant}", "deploy.ready", {"variant": cfg.variant}
            )
            return True
        self._set_status(
            "error",
            "Не удалось перезапустить движок после правки глоссария",
            "glossary.restart_failed",
        )
        return False

    # ------------------------------------------------------------------ lifecycle

    async def restore_on_boot(self) -> None:
        self._warn_about_env_divergence()
        if not self.settings.autostart:
            self._set_status("stopped", "Автозапуск отключён (AUTOSTART=0)", "idle.autostart_off")
            return
        target = self._desired or self._last_good
        if target is None:
            self._set_status(
                "stopped", "Модель не выбрана — нажмите «Развернуть»", "idle.no_model"
            )
            return
        self.bus.publish_log(f"restoring last deployment: {target.variant}")
        await self.deploy(target)

    def _warn_about_env_divergence(self) -> None:
        """Сказать в лог, что `.env` просит одно, а разворачивается другое.

        Уровня "предупреждение" у шины событий нет, поэтому строка помечается словом:
        молчаливое расхождение и было дефектом - `HOTWORDS_DEFAULT=1` не доходил до
        движка, и узнать об этом было негде.
        """
        diverging = self.env_divergence()
        if not diverging:
            return
        listed = ", ".join(
            f"{name}: .env={values['env']} / развернуто={values['state']}"
            for name, values in diverging.items()
        )
        self.bus.publish_log(
            f"ВНИМАНИЕ: .env расходится с сохраненной конфигурацией ({listed}); "
            "разворачивается сохраненная - примените .env кнопкой в консоли"
        )

    async def stop_engine(self) -> None:
        await wait_uninterrupted(asyncio.create_task(self._stop_engine()))

    async def _stop_engine(self) -> None:
        interrupted = None
        if (task := self._native_reload_task) is not None and not task.done():
            interrupted = self.current
            if not task.cancelling():
                task.cancel()
        async with self._lock:
            await self._cancel_recovery()
            running = self.current or interrupted
            await self.process.stop()
            self._set_status("stopped", "Движок остановлен", "idle.stopped")
            self.journal.record("idle.stopped", uuid4(), variant=running.variant if running else None)

    async def _cancel_recovery(self) -> None:
        if self._restart_task is not None:
            self._restart_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_task
            self._restart_task = None
        self._consecutive_failures = 0
        self._backoff_index = 0

    async def shutdown(self) -> None:
        self._shutting_down = True
        await self._cancel_comparison()
        await self._cancel_recovery()
        try:
            await self.process.stop()
        finally:
            # С остановленным процессом больше нет сборки ONNX, которую нужно ждать.
            if (task := self._native_reload_task) is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if (glossary := self._glossary_task) is not None:
                await asyncio.gather(glossary, return_exceptions=True)

    # ------------------------------------------------------------------ watchdog

    def _on_engine_exit(self, code: int) -> None:
        # Сбой до готовности обрабатывает ветка запуска, а не отдельный сторож.
        if self._shutting_down or self._status == "starting":
            return
        self.restart_count += 1
        self._set_status(
            "error",
            f"Движок неожиданно завершился (код {code}), поднимаю заново",
            "watchdog.exited",
            {"code": code},
        )
        start_recovery = self._restart_task is None or self._restart_task.done()
        if start_recovery or self._recovery_operation is None:
            self._recovery_operation = uuid4()
        exited = self.process.config
        self.journal.record(
            "watchdog.exited", self._recovery_operation, variant=exited.variant if exited else None
        )
        if start_recovery:
            self._restart_task = asyncio.create_task(self._restart_loop(self._recovery_operation))

    async def _restart_loop(self, operation_id: UUID) -> None:
        while not self._shutting_down:
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                delay: float = FAILED_RETRY_DELAY
            else:
                delay = BACKOFF_DELAYS[min(self._backoff_index, len(BACKOFF_DELAYS) - 1)]
            self.bus.publish_log(f"restarting engine in {delay:.0f}s")
            await asyncio.sleep(delay)
            if self._shutting_down:
                return
            async with self._lock:
                # Неудачная загрузка меняет desired, но не модель работающего процесса.
                cfg = self._last_good or self._desired
                if self.process.is_running or cfg is None:
                    return
                attempt = self._consecutive_failures + 1
                self._deploying = cfg.variant
                self._set_status(
                    "starting",
                    f"Поднимаю движок заново ({cfg.variant})",
                    "watchdog.restarting",
                    {"variant": cfg.variant},
                )
                self.journal.record(
                    "watchdog.restarting", operation_id, variant=cfg.variant, attempt=attempt
                )
                if await self._start_and_wait(cfg, STARTUP_TIMEOUT):
                    self._consecutive_failures = 0
                    self._backoff_index = 0
                    self._set_status(
                        "ready",
                        f"Готово: {cfg.variant} (после перезапуска)",
                        "watchdog.ready",
                        {"variant": cfg.variant},
                    )
                    self.journal.record(
                        "watchdog.ready", operation_id, variant=cfg.variant, attempt=attempt
                    )
                    return
                self._consecutive_failures += 1
                self._backoff_index += 1
                self._set_status(
                    "error",
                    f"Движок не поднялся (попытка {self._consecutive_failures}), пробую снова",
                    "watchdog.failed",
                    {"attempt": self._consecutive_failures},
                )
                self.journal.record(
                    "watchdog.failed", operation_id, variant=cfg.variant, attempt=attempt
                )
