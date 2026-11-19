"""OpenAI-compatible facade in front of the engine.

The engine already speaks the OpenAI transcription API, so the console adds only
what it lacks: an API key, a model listing, human-readable failures when nothing is
deployed yet, an upload limit, and latency measurement. The engine's own JSON is
never rewritten and uploads reach it byte for byte, browser WebM included since
2.17.0 — the console reads the length of one to time it, and changes nothing.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile
from starlette.types import Message, Receive, Scope, Send

from .auth import require_api_key
from .catalog import HEADS, openai_model_id
from .errors import ApiError
from .wavinfo import wav_duration_seconds
from .webminfo import webm_duration_seconds

router = APIRouter(dependencies=[Depends(require_api_key)])

TRANSCRIBE_TIMEOUT = httpx.Timeout(None, connect=10.0)

# Поля, которые клиент присылает, а движок не читает. `prompt` здесь не мелочь:
# у Whisper это и есть носитель пользовательского словаря, и клиенты диктовки
# (OpenWhispr и родня) шлют его на любой самохостный адрес, считая нас
# Whisper-семейством. Движок - трансдьюсер, контекст задается глоссарием на
# сервере, и молчаливое 200 в ответ означало бы, что словарь клиента не работает
# и никто об этом не узнает. Заголовок - единственный канал до чужого приложения.
IGNORED_FIELDS = ("prompt",)
GLOSSARY_SOURCE = "server-hotwords"
HOP_BY_HOP = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "host",
    "authorization",
}


def require_ready(request: Request) -> None:
    supervisor = request.app.state.supervisor
    if supervisor.status == "ready":
        return
    hints = {
        "stopped": "Модель ещё не развёрнута — откройте веб-интерфейс и нажмите «Развернуть».",
        "downloading": "Модель скачивается, это делается один раз. Повторите запрос позже.",
        "starting": "Движок запускается, обычно это пара секунд.",
        "error": f"Движок не работает: {supervisor.detail}",
    }
    raise ApiError(
        503,
        hints.get(supervisor.status, "Движок недоступен."),
        "engine_not_ready",
        type_="server_error",
        headers={"Retry-After": "5"},
    )


class _BodyLimitExceeded(OSError):
    """При OSError multipart-парсер закрывает уже созданные временные файлы."""


@asynccontextmanager
async def _bounded_request(request: Request, body_limit: int) -> AsyncIterator[Request]:
    limit = request.app.state.settings.max_upload_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > body_limit:
        raise ApiError(413, _too_big(limit), "file_too_large")
    received = 0

    async def receive() -> Message:
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > body_limit:
            raise _BodyLimitExceeded
        return message

    bounded = Request(request.scope, receive=receive)
    try:
        yield bounded
    except _BodyLimitExceeded:
        raise ApiError(413, _too_big(limit), "file_too_large") from None
    finally:
        await bounded.close()


@asynccontextmanager
async def parse_upload(request: Request) -> AsyncIterator[tuple[UploadFile, dict[str, str]]]:
    limit = request.app.state.settings.max_upload_bytes
    async with _bounded_request(request, limit + 8192) as bounded:
        form = await bounded.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "Не передан аудиофайл в поле file.", "missing_file")
        if upload.size is not None and upload.size > limit:
            raise ApiError(413, _too_big(limit), "file_too_large")
        fields = {k: v for k, v in form.multi_items() if k != "file" and isinstance(v, str)}
        yield upload, fields


async def read_upload(request: Request) -> tuple[bytes, str, str, dict[str, str]]:
    async with parse_upload(request) as (upload, fields):
        return (
            await upload.read(), upload.filename or "audio.wav",
            upload.content_type or "application/octet-stream", fields,
        )


def upload_duration_seconds(data: bytes) -> float | None:
    """How long the recording is, for the real-time factor — None when unreadable.

    Only the two containers the console can measure without decoding: WAV, which the
    page records itself, and WebM/Opus, which is what every browser dictation client
    sends. Everything else the engine decodes but nobody here measures, so its
    requests contribute wall time to the metrics and no RTF.
    """
    return wav_duration_seconds(data) or webm_duration_seconds(data)


def _too_big(limit: int) -> str:
    return (
        f"Файл больше допустимых {limit // (1024 * 1024)} МБ. "
        "Увеличьте MAX_UPLOAD_MB в .env или разрежьте запись."
    )


def ignored_field_headers(fields: dict[str, str]) -> dict[str, str]:
    """Заголовки ответа про поля запроса, которые никуда не дошли.

    Пусто, когда таких полей нет: обвешивать заголовками каждый ответ ради
    сообщения, которое почти всегда неприменимо, значило бы приучить его
    игнорировать.
    """
    ignored = [name for name in IGNORED_FIELDS if fields.get(name, "").strip()]
    if not ignored:
        return {}
    return {
        "X-Ignored-Fields": ", ".join(ignored),
        "X-Glossary-Source": GLOSSARY_SOURCE,
    }


def _warn_once(request: Request, ignored: str) -> None:
    """Одна строка в логи на весь процесс, а не на каждый запрос.

    Клиент, настроенный слать словарь в `prompt`, шлет его всегда, и запись на
    каждую диктовку превратила бы раздел «Логи» в один повторяющийся абзац -
    ровно тот способ, которым предупреждение перестает читаться.
    """
    state = request.app.state
    if getattr(state, "ignored_fields_warned", False):
        return
    state.ignored_fields_warned = True
    state.bus.publish_log(
        f"client sent {ignored}: the engine ignores it, "
        "the glossary is configured on the server"
    )


class _EngineStream(StreamingResponse):
    def __init__(self, client: httpx.AsyncClient, upstream: httpx.Response,
                 body: AsyncIterator[bytes], headers: dict[str, str]) -> None:
        super().__init__(body, status_code=upstream.status_code, headers=headers)
        self.client = client
        self.upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # BackgroundTask не вызывается, если upstream или отправка ответа упали.
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.upstream.aclose()
            finally:
                await self.client.aclose()


async def forward_transcription(
    request: Request,
    path: str = "/v1/audio/transcriptions",
) -> StreamingResponse:
    # Секундомер запускается до чтения загрузки: пользователь ждёт всю дорогу, а
    # не только ту её часть, где работает движок.
    started = time.perf_counter()
    require_ready(request)
    data, filename, content_type, fields = await read_upload(request)
    settings = request.app.state.settings
    metrics = request.app.state.metrics

    duration = upload_duration_seconds(data)

    client = httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT, trust_env=False)
    engine_request = client.build_request(
        "POST",
        f"{settings.engine_base_url}{path}",
        data=fields,
        files={"file": (filename, data, content_type)},
    )
    try:
        response = await client.send(engine_request, stream=True)
    except asyncio.CancelledError:
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise ApiError(
            502,
            f"Движок не ответил: {exc}",
            "engine_unreachable",
            type_="server_error",
        ) from exc

    async def body():
        async for chunk in response.aiter_raw():
            yield chunk
        if response.status_code < 400:
            metrics.record(duration, time.perf_counter() - started, filename)

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    ignored = ignored_field_headers(fields)
    if ignored:
        _warn_once(request, ignored["X-Ignored-Fields"])
    headers.update(ignored)
    return _EngineStream(client, response, body(), headers)


@router.post("/v1/audio/transcriptions")
async def transcriptions(request: Request) -> StreamingResponse:
    return await forward_transcription(request)


@router.post("/v1/audio/translations")
async def translations(_request: Request) -> JSONResponse:
    raise ApiError(
        400,
        "GigaAM распознаёт речь, но не переводит её. Используйте "
        "/v1/audio/transcriptions — текст будет на языке записи.",
        "translations_not_supported",
    )


@router.get("/v1/models")
async def models(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    current = supervisor.current
    created = int(time.time())
    data = [
        {
            "id": openai_model_id(head.id),
            "object": "model",
            "created": created,
            "owned_by": "gigaam",
            "deployed": current is not None and current.variant == head.id,
        }
        for head in HEADS.values()
    ]
    # Clients that hardcode a Whisper model name still work: the engine ignores the
    # `model` field and serves whatever head is deployed.
    data.append(
        {
            "id": "whisper-1",
            "object": "model",
            "created": created,
            "owned_by": "gigaam",
            "deployed": current is not None,
        }
    )
    return {"object": "list", "data": data}


def _passthrough_headers(response: httpx.Response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower() not in HOP_BY_HOP}


async def _native_reload(
    request: Request, url: str, params: list[tuple[str, str]], body: bytes, headers: dict[str, str],
) -> Response:
    async def reload() -> Response:
        require_ready(request)
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT, trust_env=False) as client:
            try:
                async with client.stream("POST", url, params=params, content=body, headers=headers) as response:
                    content = b"".join([chunk async for chunk in response.aiter_raw()])
                    return Response(
                        content, status_code=response.status_code, headers=_passthrough_headers(response),
                    )
            except httpx.HTTPError as exc:
                raise ApiError(
                    502, f"Движок не ответил: {exc}", "engine_unreachable", type_="server_error"
                ) from exc

    response = await request.app.state.supervisor.run_native_reload(reload)
    if response is not None:
        return response
    # Такой же конфликт, как у upstream, вместо очереди повторных reload.
    return JSONResponse(
        {"error": "A model reload is already in progress", "code": "reload_in_progress"},
        status_code=409,
    )


# `include_in_schema=False`: одна запись с подстановкой `{path}` ничего не сообщает
# читателю схемы, а три метода на одном маршруте дают FastAPI повторяющийся
# operation id и предупреждение при каждой сборке схемы. Родные эндпоинты движка
# описаны в README, документация у них своя — апстримная.
@router.api_route(
    "/v1/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False
)
async def passthrough(request: Request, path: str) -> Response:
    """Everything else the engine exposes (native /v1/transcribe, /v1/jobs, ...)."""
    require_ready(request)
    settings = request.app.state.settings
    url = f"{settings.engine_base_url}/v1/{path}"
    async with _bounded_request(request, settings.engine_body_limit_bytes) as bounded:
        body = await bounded.body()
    # Ключ консоли не должен попасть в URL движка и технические логи.
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "api_key"]
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP
    }
    # Проверяем путь после нормализации HTTPX, а не исходный сегмент catch-all.
    if request.method == "POST" and httpx.URL(url).path == "/v1/admin/reload":
        return await _native_reload(request, url, params, body, headers)

    client = httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT, trust_env=False)
    engine_request = client.build_request(
        request.method,
        url,
        params=params,
        content=body,
        headers=headers,
    )
    try:
        response = await client.send(engine_request, stream=True)
    except asyncio.CancelledError:
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise ApiError(
            502, f"Движок не ответил: {exc}", "engine_unreachable", type_="server_error"
        ) from exc

    return _EngineStream(client, response, response.aiter_raw(), _passthrough_headers(response))
