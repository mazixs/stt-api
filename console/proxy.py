"""OpenAI-compatible facade in front of the engine.

The engine already speaks the OpenAI transcription API, so the console adds only
what it lacks: an API key, a model listing, human-readable failures when nothing is
deployed yet, an upload limit, and latency measurement. The engine's own JSON is
never rewritten and uploads reach it byte for byte, browser WebM included since
2.17.0 — the console reads the length of one to time it, and changes nothing.
"""

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .auth import require_api_key
from .catalog import HEADS, openai_model_id
from .errors import ApiError
from .wavinfo import wav_duration_seconds
from .webminfo import webm_duration_seconds

router = APIRouter(dependencies=[Depends(require_api_key)])

TRANSCRIBE_TIMEOUT = httpx.Timeout(None, connect=10.0)
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


async def read_upload(request: Request) -> tuple[bytes, str, str, dict[str, str]]:
    limit = request.app.state.settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 8192:
        raise ApiError(413, _too_big(limit), "file_too_large")

    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise ApiError(400, "Не передан аудиофайл в поле file.", "missing_file")
    data = await upload.read()
    if len(data) > limit:
        raise ApiError(413, _too_big(limit), "file_too_large")
    fields = {
        key: value
        for key, value in form.multi_items()
        if key != "file" and isinstance(value, str)
    }
    return (
        data,
        upload.filename or "audio.wav",
        upload.content_type or "application/octet-stream",
        fields,
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

    client = httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT)
    engine_request = client.build_request(
        "POST",
        f"{settings.engine_base_url}{path}",
        data=fields,
        files={"file": (filename, data, content_type)},
    )
    try:
        response = await client.send(engine_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise ApiError(
            502,
            f"Движок не ответил: {exc}",
            "engine_unreachable",
            type_="server_error",
        ) from exc

    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            if response.status_code < 400:
                metrics.record(duration, time.perf_counter() - started, filename)

    async def cleanup():
        await response.aclose()
        await client.aclose()

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("content-type"),
        background=BackgroundTask(cleanup),
    )


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


# `include_in_schema=False`: одна запись с подстановкой `{path}` ничего не сообщает
# читателю схемы, а три метода на одном маршруте дают FastAPI повторяющийся
# operation id и предупреждение при каждой сборке схемы. Родные эндпоинты движка
# описаны в README, документация у них своя — апстримная.
@router.api_route(
    "/v1/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False
)
async def passthrough(request: Request, path: str) -> StreamingResponse:
    """Everything else the engine exposes (native /v1/transcribe, /v1/jobs, ...)."""
    require_ready(request)
    settings = request.app.state.settings
    url = f"{settings.engine_base_url}/v1/{path}"
    body = await request.body()
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP
    }

    client = httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT)
    engine_request = client.build_request(
        request.method,
        url,
        params=dict(request.query_params),
        content=body,
        headers=headers,
    )
    try:
        response = await client.send(engine_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise ApiError(
            502, f"Движок не ответил: {exc}", "engine_unreachable", type_="server_error"
        ) from exc

    async def cleanup():
        await response.aclose()
        await client.aclose()

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_BY_HOP
        },
        media_type=response.headers.get("content-type"),
        background=BackgroundTask(cleanup),
    )
