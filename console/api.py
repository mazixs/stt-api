"""Management API used by the web console.

Everything the UI needs and nothing the engine already provides: status, catalog,
deploy/stop, glossary, a live event stream and a one-off test transcription that
reports its own timing.
"""

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import owner_access
from .auth import require_api_key
from .catalog import DEFAULT_LANG, HEADS, LANGS, is_downloaded, pick
from .errors import ApiError
from .glossary import entry_issues, parse_context, read_glossary
from .proxy import (
    TRANSCRIBE_TIMEOUT,
    models as client_models,
    read_upload,
    require_ready,
    upload_duration_seconds,
)
from .settings import HeadId, Mode
from .state import EngineConfig
from .vocab import head_alphabet, split_representable

router = APIRouter()
admin = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
admin.add_api_route("/client-models", client_models, methods=["GET"])
STARTED_AT = time.monotonic()
_PUBLIC_ENGINE_DETAIL = {
    "stopped": "Движок остановлен",
    "downloading": "Загрузка модели",
    "starting": "Движок запускается",
    "ready": "Движок готов",
    "error": "Ошибка движка",
}


class DeployRequest(BaseModel):
    variant: HeadId
    punctuation: Mode | None = None
    itn: Mode | None = None
    vad: bool | None = None
    pool_size: int | None = Field(default=None, ge=1, le=8)
    hotwords_boost: float | None = Field(default=None, ge=0.0, le=50.0)
    hotwords_default: bool | None = None
    file_window_concurrency: int | None = Field(default=None, ge=1, le=8)

    def to_config(self, defaults: EngineConfig) -> EngineConfig:
        return EngineConfig(
            variant=self.variant,
            punctuation=self.punctuation or defaults.punctuation,
            itn=self.itn or defaults.itn,
            vad=defaults.vad if self.vad is None else self.vad,
            pool_size=self.pool_size or defaults.pool_size,
            hotwords_boost=(
                defaults.hotwords_boost if self.hotwords_boost is None else self.hotwords_boost
            ),
            hotwords_default=(
                defaults.hotwords_default
                if self.hotwords_default is None
                else self.hotwords_default
            ),
            file_window_concurrency=(
                self.file_window_concurrency or defaults.file_window_concurrency
            ),
        )


class GlossaryRequest(BaseModel):
    text: str = ""


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness of the console itself.

    Always 200 while the process is alive: Docker's healthcheck must not restart the
    container just because no model is deployed yet.
    """
    supervisor = request.app.state.supervisor
    return {
        "status": "ok",
        "engine_status": supervisor.status,
        # Свободный текст ошибок, включая пути ОС, остаётся в закрытом /api/status.
        "engine_detail": _PUBLIC_ENGINE_DETAIL.get(supervisor.status, "Состояние движка неизвестно"),
        "engine_detail_code": supervisor.detail_code,
        "variant": supervisor.current.variant if supervisor.current else None,
    }


def _status_payload(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    settings = request.app.state.settings
    current = supervisor.current
    return {
        "status": supervisor.status,
        # `detail` - готовая строка по-русски, она же идет в логи. `detail_code` и
        # `detail_params` - то же самое машиночитаемо, чтобы консоль показала фразу
        # на выбранном языке, а не переводила текст обратно.
        "detail": supervisor.detail,
        "detail_code": supervisor.detail_code,
        "detail_params": supervisor.detail_params,
        "download_percent": supervisor.download_percent,
        # Цель развертывания, а не работающий процесс: консоль рисует прогресс на
        # карточке той головы, которую разворачивают, и до первого запуска процесса нет.
        "deploying": supervisor.deploying,
        "engine": {
            "variant": current.variant if current else None,
            "punctuation": current.punctuation if current else None,
            "itn": current.itn if current else None,
            "vad": current.vad if current else None,
            "pool_size": current.pool_size if current else None,
            "hotwords_boost": current.hotwords_boost if current else None,
            "hotwords_default": current.hotwords_default if current else None,
            "file_window_concurrency": current.file_window_concurrency if current else None,
            "running": supervisor.process.is_running,
        },
        "defaults": supervisor.default_config().to_dict(),
        # Состояние сильнее `.env` намеренно, поэтому расхождение между ними не
        # исправляется само, а показывается: что просит файл, что из этого задано явно
        # и где значения разошлись.
        "env": {
            "config": supervisor.env_config().to_dict(),
            "explicit": sorted(settings.explicit_engine_fields()),
            "diverges": supervisor.env_divergence(),
        },
        "metrics": request.app.state.metrics.snapshot(),
        "api_key_set": settings.owner_access or bool(settings.api_key),
        "glossary_count": supervisor.glossary_count,
        "restart_count": supervisor.restart_count,
        "uptime_seconds": int(time.monotonic() - STARTED_AT),
        "max_upload_mb": settings.max_upload_mb,
        "compare_enabled": settings.compare_enabled,
    }


@admin.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _status_payload(request)


@admin.get("/models")
async def models(request: Request, lang: str = DEFAULT_LANG) -> dict[str, Any]:
    """Каталог голов на языке интерфейса.

    Переводятся только пояснения: идентификаторы, названия моделей и коды языков
    одинаковы везде. Неизвестный `lang` молча становится английским - выбор языка
    в интерфейсе не повод отвечать ошибкой.
    """
    settings = request.app.state.settings
    current = request.app.state.supervisor.current
    if lang not in LANGS:
        lang = DEFAULT_LANG
    return {
        "lang": lang,
        "heads": [
            {
                "id": head.id,
                "title": head.title,
                "subtitle": pick(head.subtitle, lang),
                "languages": list(head.languages),
                "native_punctuation": head.native_punctuation,
                "size_mb": head.size_mb,
                "badge": pick(head.badge, lang),
                "badge_note": pick(head.badge_note, lang),
                "downloaded": is_downloaded(head.id, settings.model_dir),
                "deployed": current is not None and current.variant == head.id,
            }
            for head in HEADS.values()
        ]
    }


@admin.post("/deploy", status_code=202)
async def deploy(request: Request, body: DeployRequest) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    config = body.to_config(supervisor.default_config())
    task = asyncio.create_task(supervisor.deploy(config))
    # При остановке надо дождаться и текущего развёртывания, и стоящих за ним.
    pending = request.app.state.deploy_tasks
    pending.add(task)
    task.add_done_callback(pending.discard)
    request.app.state.deploy_task = task
    return {"status": "accepted", "config": config.to_dict()}


@admin.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    await request.app.state.supervisor.stop_engine()
    return {"status": "stopped"}


@admin.get("/glossary")
async def glossary_get(request: Request) -> dict[str, Any]:
    """The glossary plus how much of it the current head can actually spell."""
    supervisor = request.app.state.supervisor
    settings = request.app.state.settings
    defaults = supervisor.default_config()
    current = supervisor.current
    variant = current.variant if current else defaults.variant

    text = read_glossary(settings.hotwords_path)
    entries = parse_context(text)
    alphabet = head_alphabet(variant, settings.model_dir)
    usable, dropped = (
        split_representable([phrase for phrase, _weight in entries], alphabet)
        if alphabet is not None
        else ([], [])
    )
    return {
        "text": text,
        "count": len(entries),
        "boost": defaults.hotwords_boost,
        "variant": variant,
        # None rather than 0 when the vocabulary is unreadable: the UI must not
        # claim phrases are being dropped when we simply do not know.
        "usable_count": len(usable) if alphabet is not None else None,
        "dropped": dropped,
        # Фразы из INITIAL_CONTEXT, которых в списке нет: `.env` читается один раз, при
        # создании файла, поэтому дальше расхождение видно только так.
        "env_missing": supervisor.initial_context_missing(),
        # Что движок сделает с фразой помимо того, о чем его просили: вес <= 0
        # выбрасывает фразу, любой положительный вес равен единице, строка с `#`
        # читается как комментарий. Коды, а не фразы: формулировку выбирает
        # консоль на своем языке.
        "issues": entry_issues(text),
        "approximate": alphabet is not None and alphabet.subword,
        # What this head can spell, so the advice is read off the vocabulary on disk
        # instead of a rule that is wrong for some head: `e2e_rnnt` writes Latin and
        # digits, `rnnt` writes neither. Case is deliberately absent — the engine
        # retries a phrase lowercased itself, so it can never be the reason one dies.
        "alphabet": (
            None
            if alphabet is None
            else {
                "latin": alphabet.writes_latin,
                "digits": alphabet.writes_digits,
            }
        ),
    }


@admin.post("/glossary")
async def glossary_post(request: Request, body: GlossaryRequest) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    applied = await supervisor.apply_glossary(body.text)
    # Претензии считаются по присланному тексту, а не по сохраненному файлу:
    # файл часть из них не переживает. Неразобранный вес в нем неотличим от
    # отсутствующего, а табуляцию `render_hotwords` убирает, иначе она разрезала
    # бы строку. Сказать об этом можно только здесь, в ответ на саму правку.
    return {
        "count": supervisor.glossary_count,
        "applied": applied,
        "issues": entry_issues(body.text),
    }


@admin.get("/journal")
async def journal(request: Request) -> dict[str, Any]:
    """До 200 событий развёртывания и движка, новые сверху; только за жизнь процесса."""
    return {"events": request.app.state.supervisor.journal.snapshot()}


@admin.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus = request.app.state.bus
    subscription = bus.subscribe()
    snapshot = _status_payload(request)
    log_lines = bus.log_lines(limit=50)

    async def stream():
        try:
            yield _sse({"type": "snapshot", **snapshot})
            for line in log_lines:
                yield _sse({"type": "log", "line": line})
            if not request.app.state.settings.owner_access:
                async for event in subscription:
                    yield _sse(event)
                return
            loop = asyncio.get_running_loop()
            deadline = loop.time() + owner_access.SSE_CHECK_SECONDS
            while True:
                if loop.time() >= deadline:
                    try:
                        if not await owner_access.live_session(request):
                            return
                    except ApiError:
                        return
                    deadline = loop.time() + owner_access.SSE_CHECK_SECONDS
                try:
                    async with asyncio.timeout_at(deadline):
                        event = await anext(subscription)
                except TimeoutError:
                    continue
                yield _sse(event)
        finally:
            await subscription.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@admin.post("/test")
async def test_transcription(request: Request) -> JSONResponse:
    """Transcribe one file and report how long it took on this machine."""
    started = time.perf_counter()
    require_ready(request)
    data, filename, content_type, fields = await read_upload(request)
    settings = request.app.state.settings
    metrics = request.app.state.metrics

    payload = {"model": "whisper-1", "response_format": "json"}
    payload.update({k: v for k, v in fields.items() if k in ("language", "response_format")})

    try:
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT, trust_env=False) as client:
            response = await client.post(
                f"{settings.engine_base_url}/v1/audio/transcriptions",
                data=payload,
                files={"file": (filename, data, content_type)},
            )
    except httpx.HTTPError as exc:
        raise ApiError(
            502, f"Движок не ответил: {exc}", "engine_unreachable", type_="server_error"
        ) from exc
    elapsed = time.perf_counter() - started

    if response.status_code >= 400:
        return JSONResponse(status_code=response.status_code, content=_engine_error(response))

    text = ""
    try:
        body = response.json()
        text = body.get("text", "") if isinstance(body, dict) else ""
    except ValueError:
        text = response.text

    audio_seconds = upload_duration_seconds(data)
    metrics.record(audio_seconds, elapsed, filename)
    return JSONResponse(
        {
            "text": text,
            "elapsed": round(elapsed, 3),
            "audio_seconds": audio_seconds,
            "rtf": round(elapsed / audio_seconds, 3) if audio_seconds else None,
        }
    )


@admin.get("/docs", include_in_schema=False)
async def swagger_ui(request: Request) -> HTMLResponse:
    """Swagger UI под тем же ключом, что и остальной `/api`.

    Схема описывает управляющие эндпоинты вместе с их телами запросов, поэтому она
    закрывается ровно тогда, когда закрыт сам сервис: пустой `API_KEY` — открыто,
    заданный — нужен ключ. Отдельного переключателя для документации нет намеренно,
    иначе владелец сервиса решал бы один вопрос дважды.

    Ключ принимается ещё и как `?api_key=`, как на SSE: браузер не умеет послать
    заголовок при переходе по ссылке. Пришедший так ключ страница подставляет в ссылку
    на схему — иначе Swagger получил бы 401 на первом же запросе к `/api/openapi.json`.
    """
    if request.app.state.settings.owner_access:
        return HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Камертон — API</title>'
            '<main><h1>Камертон — API</h1><p>Клиентские приложения используют Bearer-ключ. '
            'Управление консолью доступно по сессии владельца.</p>'
            '<a href="/api/openapi.json">Открыть схему OpenAPI (JSON)</a></main></html>',
            headers={"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"},
        )
    presented = request.query_params.get("api_key", "")
    openapi_url = "/api/openapi.json"
    if presented:
        openapi_url += f"?api_key={quote(presented, safe='')}"
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title="GigaAM STT Console — API",
        oauth2_redirect_url=None,
    )


@admin.get("/openapi.json", include_in_schema=False)
async def openapi_schema(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi())


def _engine_error(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"message": response.text[:500], "code": "engine_error"}}
    if isinstance(body, dict) and "error" in body:
        return body
    return {"error": {"message": str(body)[:500], "code": "engine_error"}}


router.include_router(admin)
