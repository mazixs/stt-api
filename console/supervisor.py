"""Deployment state machine and crash watchdog.

Owns the answers to three questions the user cares about:
  * what is the engine doing right now (status + detail, streamed to the UI),
  * what should it be doing after a restart (persisted desired config),
  * what happens when something breaks (rollback to the last good config, and
    automatic restarts with backoff when the engine dies on its own).
"""

import asyncio
import contextlib
from typing import Any

from .downloader import DownloadError, download
from .engine import EngineProcess, EngineStartupError
from .events import EventBus
from .glossary import parse_context, read_glossary, write_hotwords
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
        self.state_file = StateFile(settings.state_path)
        stored = self.state_file.load()

        self._status = "stopped"
        self._detail = ""
        self._desired: EngineConfig | None = stored.desired
        self._last_good: EngineConfig | None = stored.last_good
        self._download_percent: int | None = None

        self.process = EngineProcess(
            settings,
            on_log=self.bus.publish_log,
            on_exit=self._on_engine_exit,
        )
        self.restart_count = 0
        self._start_error = ""
        self._lock = asyncio.Lock()
        self._shutting_down = False
        self._consecutive_failures = 0
        self._backoff_index = 0
        self._restart_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> str:
        return self._status

    @property
    def detail(self) -> str:
        return self._detail

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

    def _set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        self._detail = detail
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
                "variant": self.current.variant if self.current else None,
                "percent": self._download_percent,
            }
        )

    # ------------------------------------------------------------------ deploy

    async def deploy(self, cfg: EngineConfig, startup_timeout: float = STARTUP_TIMEOUT) -> None:
        async with self._lock:
            await self._deploy_locked(cfg, startup_timeout)

    async def _deploy_locked(self, cfg: EngineConfig, startup_timeout: float) -> None:
        self._desired = cfg
        self._download_percent = None
        self._set_status("downloading", f"Проверяю и скачиваю модель ({cfg.variant})")
        if not await self._download_with_retries(cfg):
            return
        self._download_percent = None

        self._seed_hotwords()
        await self.process.stop()
        self._set_status("starting", f"Запускаю движок ({cfg.variant})")
        started = await self._start_and_wait(cfg, startup_timeout)
        if started:
            self._last_good = cfg
            self._consecutive_failures = 0
            self._backoff_index = 0
            self._set_status("ready", f"Готово: {cfg.variant}")
            return
        await self._rollback(cfg, startup_timeout)

    async def _download_with_retries(self, cfg: EngineConfig) -> bool:
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
                    )
                    await asyncio.sleep(delay)
                    continue
                running = self.current
                detail = exc.message
                if attempt > 1:
                    detail += f" Попыток было {attempt}."
                if running is not None:
                    detail += f" Продолжает работать прежняя модель ({running.variant})."
                self._set_status("error", detail)
                return False
        return False

    async def _start_and_wait(self, cfg: EngineConfig, timeout: float) -> bool:
        try:
            await self.process.start(cfg)
            await self.process.wait_healthy(timeout=timeout)
            return True
        except (EngineStartupError, OSError, RuntimeError) as exc:
            self.bus.publish_log(f"engine failed to start: {exc}")
            self._start_error = str(exc)
            await self.process.stop()
            return False

    async def _rollback(self, failed: EngineConfig, startup_timeout: float) -> None:
        reason = self._start_error or "движок не запустился"
        target = self._last_good
        if target is None or target == failed:
            self._set_status("error", f"Не удалось запустить модель {failed.variant}: {reason}")
            return
        self._set_status("starting", f"Откат на предыдущую модель ({target.variant})")
        if await self._start_and_wait(target, startup_timeout):
            self._desired = target
            self._set_status(
                "ready",
                f"Откат на предыдущую модель ({target.variant}): "
                f"модель {failed.variant} не запустилась — {reason}",
            )
            return
        self._set_status(
            "error",
            f"Модель {failed.variant} не запустилась ({reason}), откат на "
            f"{target.variant} тоже не удался.",
        )

    def _on_download_event(self, event: dict[str, Any]) -> None:
        phase = event.get("phase")
        if phase == "progress":
            self._download_percent = int(event.get("percent") or 0)
            self.bus.publish({"type": "download", "percent": self._download_percent})
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
        count = write_hotwords(self.settings.hotwords_path, raw)
        self.bus.publish_log(f"glossary updated: {count} phrases")
        if not self.process.is_running:
            return True
        if await self.process.reload():
            self.bus.publish({"type": "glossary", "count": count})
            return True
        self.bus.publish_log("engine reload failed, restarting the engine instead")
        cfg = self.process.config or self.default_config()
        async with self._lock:
            await self.process.stop()
            self._set_status("starting", "Перезапускаю движок для нового глоссария")
            if await self._start_and_wait(cfg, STARTUP_TIMEOUT):
                self._set_status("ready", f"Готово: {cfg.variant}")
                return True
            self._set_status("error", "Не удалось перезапустить движок после правки глоссария")
            return False

    # ------------------------------------------------------------------ lifecycle

    async def restore_on_boot(self) -> None:
        self._warn_about_env_divergence()
        if not self.settings.autostart:
            self._set_status("stopped", "Автозапуск отключён (AUTOSTART=0)")
            return
        target = self._desired or self._last_good
        if target is None:
            self._set_status("stopped", "Модель не выбрана — нажмите «Развернуть»")
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
        async with self._lock:
            await self.process.stop()
            self._set_status("stopped", "Движок остановлен")

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_task
            self._restart_task = None
        await self.process.stop()

    # ------------------------------------------------------------------ watchdog

    def _on_engine_exit(self, code: int) -> None:
        if self._shutting_down:
            return
        self.restart_count += 1
        self._set_status("error", f"Движок неожиданно завершился (код {code}), поднимаю заново")
        if self._restart_task is None or self._restart_task.done():
            self._restart_task = asyncio.create_task(self._restart_loop())

    async def _restart_loop(self) -> None:
        while not self._shutting_down:
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                delay: float = FAILED_RETRY_DELAY
            else:
                delay = BACKOFF_DELAYS[min(self._backoff_index, len(BACKOFF_DELAYS) - 1)]
            self.bus.publish_log(f"restarting engine in {delay:.0f}s")
            await asyncio.sleep(delay)
            if self._shutting_down:
                return
            cfg = self._desired or self._last_good
            if cfg is None:
                return
            async with self._lock:
                if self.process.is_running:
                    return
                self._set_status("starting", f"Поднимаю движок заново ({cfg.variant})")
                if await self._start_and_wait(cfg, STARTUP_TIMEOUT):
                    self._consecutive_failures = 0
                    self._backoff_index = 0
                    self._set_status("ready", f"Готово: {cfg.variant} (после перезапуска)")
                    return
                self._consecutive_failures += 1
                self._backoff_index += 1
                self._set_status(
                    "error",
                    f"Движок не поднялся (попытка {self._consecutive_failures}), пробую снова",
                )
