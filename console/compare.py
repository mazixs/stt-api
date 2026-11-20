"""Изолированное сравнение уже скачанных голов через CLI gigastt."""

import asyncio
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.types import Message

from .auth import require_api_key
from .catalog import HEADS, is_downloaded
from .engine import build_compare_argv, wait_uninterrupted
from .errors import ApiError
from .proxy import parse_upload
from .settings import Settings

router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
UPLOAD_TIMEOUT = 60.0
COMPARE_TIMEOUT = 120.0


class _UploadAborted(OSError):
    # На OSError парсер Starlette сам закрывает ещё не возвращённые файлы.
    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error


def _disconnected() -> ApiError:
    return ApiError(499, "Клиент прервал сравнение.", "compare_disconnected")


def _engine_busy() -> ApiError:
    return ApiError(409, "Движок занят запуском или настройкой. Повторите позже.", "compare_engine_busy")


def _disabled() -> ApiError:
    return ApiError(503, "Сравнение моделей отключено.", "compare_disabled", "server_error")


async def _read_audio(request: Request, stack: AsyncExitStack) -> tuple[UploadFile, FormData]:
    deadline = asyncio.get_running_loop().time() + UPLOAD_TIMEOUT

    async def receive() -> Message:
        try:
            async with asyncio.timeout_at(deadline):
                message = await request.receive()
        except TimeoutError:
            raise _UploadAborted(
                ApiError(408, "Загрузка аудио заняла слишком много времени.", "compare_upload_timeout")
            ) from None
        if message["type"] == "http.disconnect":
            raise _UploadAborted(_disconnected())
        return message

    upload_request = Request(request.scope, receive=receive)
    try:
        return await stack.enter_async_context(parse_upload(upload_request))
    except _UploadAborted as exc:
        raise exc.error from None
    except (HTTPException, ValueError):
        raise ApiError(400, "Не удалось прочитать multipart-загрузку.", "invalid_multipart") from None
    except OSError:
        raise ApiError(500, "Не удалось обработать загрузку.", "compare_upload_error", "server_error") from None


def _temp_root(settings: Settings) -> Path:
    scope = hashlib.sha256(os.fsencode(settings.data_dir.resolve())).hexdigest()
    return Path(tempfile.gettempdir()) / f"stt-api-compare-{scope}"


def _is_private_root(root: Path) -> bool:
    try:
        info = root.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or root.is_junction() or (
        os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise OSError("Небезопасный каталог временных файлов сравнения.")
    return True


def clean_compare_temp_root(settings: Settings) -> None:
    # Чистим только собственный приватный каталог этой консоли, не общий /tmp.
    root = _temp_root(settings)
    if _is_private_root(root):
        shutil.rmtree(root)


def _cli_env() -> dict[str, str]:
    # CLI получает только окружение для запуска, без ключей и настроек рабочего движка.
    names = ("PATH", "SystemRoot", "HOME", "USERPROFILE", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
    return {name: os.environ[name] for name in names if name in os.environ} | {"NO_COLOR": "1"}


def _stage_head(source: Path, target: Path, variant: str) -> None:
    # Свой каталог на голову: CLI не берёт общий .download.lock и не пишет
    # оптимизированный граф в каталог работающего движка. Веса не копируются.
    target.mkdir()
    for name in HEADS[variant].files:
        path = source / name
        link = target / name
        if os.name == "nt":
            try:
                os.link(path, link)
                continue
            except OSError:
                pass  # Другой том: символическая ссылка, если её разрешает Windows.
        link.symlink_to(path)


async def _kill_and_reap(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    try:
        process = await spawn
    except OSError:
        return
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _run_head(
    settings: Settings, audio: Path, model_dir: Path, variant: str, output: Path,
) -> tuple[dict[str, str | float], float]:
    argv = build_compare_argv(settings, audio, model_dir, variant, output)
    env = _cli_env()
    started = time.perf_counter()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    ))
    process = None
    try:
        process = await asyncio.shield(spawn)
        if await process.wait() != 0:
            raise ValueError("gigastt transcribe failed")
        elapsed = round(time.perf_counter() - started, 3)
    finally:
        # Отмена возможна между запуском процесса и получением объекта Process.
        if process is None or process.returncode is None:
            await wait_uninterrupted(asyncio.create_task(_kill_and_reap(spawn)))

    payload = json.loads(output.read_bytes())
    text = payload.get("text") if isinstance(payload, dict) else None
    duration = payload.get("duration") if isinstance(payload, dict) else None
    if (
        not isinstance(text, str) or type(duration) not in (int, float)
        or not math.isfinite(duration) or duration < 0
    ):
        raise ValueError("invalid gigastt export")
    return {"variant": variant, "text": text, "elapsed": elapsed}, float(duration)


def _copy_audio(upload: UploadFile, audio: Path) -> None:
    with audio.open("wb") as target:
        shutil.copyfileobj(upload.file, target, length=1024 * 1024)


async def _run_comparison(settings: Settings, upload: UploadFile, a: str, b: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(COMPARE_TIMEOUT):
            source = settings.model_dir.resolve()
            if not all(is_downloaded(variant, source) for variant in (a, b)):
                raise ApiError(409, "Сначала скачайте обе модели.", "compare_model_not_downloaded")

            root = _temp_root(settings)
            root.mkdir(mode=0o700, exist_ok=True)
            _is_private_root(root)
            with tempfile.TemporaryDirectory(dir=root) as temp:
                audio = Path(temp) / "audio"
                await wait_uninterrupted(asyncio.create_task(asyncio.to_thread(_copy_audio, upload, audio)))
                await upload.close()
                results = []
                audio_seconds = None
                for variant in (a, b):
                    with tempfile.TemporaryDirectory(dir=temp) as work:
                        model_dir = Path(work) / "models"
                        _stage_head(source, model_dir, variant)
                        result, duration = await _run_head(
                            settings, audio, model_dir, variant, Path(work) / "result.json",
                        )
                        results.append(result)
                        if audio_seconds is None:
                            audio_seconds = duration
                return {"results": results, "audio_seconds": audio_seconds}
    except TimeoutError:
        raise ApiError(504, "Сравнение не завершилось вовремя.", "compare_timeout", "server_error") from None
    except (OSError, ValueError, OverflowError):
        raise ApiError(502, "Не удалось сравнить модели.", "compare_engine_error", "server_error") from None


async def _wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


@router.post("/compare")
async def compare_models(request: Request) -> dict[str, Any]:
    """Сравнить две головы (`file`, `variant_a`, `variant_b` в multipart).

    `elapsed` — секунды от запуска отдельного CLI до его завершения, включая
    загрузку модели, декодирование аудио, распознавание и запись результата.
    Передача файла и подготовка временного каталога не входят в замер.
    """
    settings: Settings = request.app.state.settings
    supervisor = request.app.state.supervisor
    if not supervisor.compare_enabled:
        raise _disabled()
    epoch = supervisor.compare_epoch
    if not supervisor.comparison_allowed:
        raise _engine_busy()
    lock: asyncio.Lock = request.app.state.compare_lock
    if lock.locked():
        raise ApiError(409, "Сравнение уже выполняется.", "compare_busy")

    async with lock, AsyncExitStack() as stack:
        upload, fields = await _read_audio(request, stack)
        if not supervisor.compare_enabled or supervisor.compare_epoch != epoch:
            raise _disabled()
        a, b = fields.get("variant_a", ""), fields.get("variant_b", "")
        del fields
        if not a or not b:
            raise ApiError(400, "Укажите обе модели.", "compare_missing_variant")
        if not isinstance(a, str) or not isinstance(b, str) or a not in HEADS or b not in HEADS:
            raise ApiError(400, "Неизвестная модель для сравнения.", "compare_unknown_variant")
        if a == b:
            raise ApiError(400, "Выберите разные модели.", "compare_same_variant")
        if upload.size == 0:
            raise ApiError(400, "Аудиофайл пуст.", "compare_empty_file")

        work = supervisor.begin_comparison(lambda: _run_comparison(settings, upload, a, b))
        if work is None:
            raise _engine_busy()
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        try:
            done, _ = await asyncio.wait((work, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                if work.cancelled():
                    raise ApiError(409, "Сравнение прервано для запуска движка.", "compare_preempted")
                return work.result()
            disconnect.result()
            raise _disconnected()
        finally:
            for task in (work, disconnect):
                if not task.done():
                    task.cancel()
            await wait_uninterrupted(asyncio.gather(work, disconnect, return_exceptions=True))
