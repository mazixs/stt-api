"""Lifecycle of the `gigastt serve` child process.

The console owns the process: it builds the command line from an `EngineConfig`,
starts it on loopback, waits until the engine reports a loaded model, and stops it
on SIGTERM with a hard kill as the backstop. Nothing here knows about audio.
"""

import asyncio
import contextlib
import signal
from collections.abc import Callable
from typing import Any

import httpx

from .downloader import engine_command, engine_env, strip_ansi
from .settings import Settings
from .state import EngineConfig


class EngineStartupError(RuntimeError):
    pass


def build_argv(settings: Settings, cfg: EngineConfig) -> list[str]:
    argv = engine_command(
        settings,
        "serve",
        "--host",
        settings.engine_host,
        "--port",
        str(settings.engine_port),
        "--bind-all",
        "--model-dir",
        str(settings.model_dir),
        "--model-variant",
        cfg.variant,
        "--punctuation",
        cfg.punctuation,
        "--itn",
        cfg.itn,
        "--pool-size",
        str(cfg.pool_size),
        "--hotwords-file",
        str(settings.hotwords_path),
        "--hotwords-boost",
        str(cfg.hotwords_boost),
        # Side models default to the engine user's home directory, which is not a
        # volume: without these two flags the punctuation and VAD models would be
        # re-downloaded on every container recreate.
        # Предел тела у движка свой, и по умолчанию он равен нашему — из-за чего
        # файл ровно в `MAX_UPLOAD_MB` не проходил: обёртка multipart добавляет
        # к нему несколько сотен байт. Считаем его от нашего лимита, чтобы
        # отказывала консоль понятным текстом, а не движок словами про multipart.
        "--body-limit-bytes",
        str(settings.engine_body_limit_bytes),
        "--punct-model-dir",
        str(settings.model_dir / "punct"),
        "--vad-model-dir",
        str(settings.model_dir / "vad"),
    )
    if cfg.vad:
        argv.append("--vad")
    if cfg.hotwords_default:
        argv.append("--hotwords-default")
    if settings.enable_jobs:
        argv.append("--enable-jobs")
    return argv


class EngineProcess:
    def __init__(
        self,
        settings: Settings,
        on_log: Callable[[str], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self.settings = settings
        self.on_log = on_log or (lambda line: None)
        self.on_exit = on_exit or (lambda code: None)
        self.config: EngineConfig | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._expected_exit = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def start(self, cfg: EngineConfig) -> None:
        if self.is_running:
            raise RuntimeError("engine already running")
        argv = build_argv(self.settings, cfg)
        env = engine_env(self.settings)
        env["RUST_LOG"] = f"gigastt={self.settings.log_level}"
        self.settings.model_dir.mkdir(parents=True, exist_ok=True)
        self.settings.hotwords_path.parent.mkdir(parents=True, exist_ok=True)

        self._expected_exit = False
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.config = cfg
        self.on_log(f"engine started: pid={self._process.pid} variant={cfg.variant}")
        self._tasks = [
            asyncio.create_task(self._pump(self._process.stdout)),
            asyncio.create_task(self._pump(self._process.stderr)),
            asyncio.create_task(self._watch(self._process)),
        ]

    async def _pump(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            async for raw in stream:
                line = strip_ansi(raw.decode("utf-8", "replace")).rstrip()
                if line:
                    self.on_log(line)

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        if self._expected_exit:
            return
        self.on_log(f"engine exited unexpectedly with code {code}")
        self.on_exit(code)

    async def stop(self, timeout: float = 15.0) -> None:
        process, self._process = self._process, None
        self._expected_exit = True
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                self.on_log("engine did not stop in time, killing")
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self.config = None

    async def health(self, timeout: float = 2.0) -> dict[str, Any] | None:
        if not self.is_running:
            return None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self.settings.engine_base_url}/health")
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def wait_healthy(self, timeout: float = 180.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not self.is_running:
                raise EngineStartupError(
                    "Движок завершился при запуске — смотрите логи в разделе «Логи»."
                )
            payload = await self.health()
            if payload and payload.get("status") == "ok" and payload.get("model") != "loading":
                return payload
            await asyncio.sleep(0.5)
        raise EngineStartupError(
            f"Движок не вышел в рабочее состояние за {int(timeout)} с "
            "(модель так и не загрузилась)."
        )

    async def reload(self, timeout: float = 60.0) -> bool:
        """Ask the engine to rebuild itself from its boot recipe (re-reads hotwords)."""
        if not self.is_running:
            return False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self.settings.engine_base_url}/v1/admin/reload")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
