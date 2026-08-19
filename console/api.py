"""Management API used by the web console.

Everything the UI needs and nothing the engine already provides: status, catalog,
deploy/stop, glossary, a live event stream and a one-off test transcription that
reports its own timing.
"""

import asyncio
import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import require_api_key
from .catalog import HEADS, is_downloaded
from .errors import ApiError
from .glossary import parse_context, read_glossary
from .proxy import (
    TRANSCRIBE_TIMEOUT,
    read_upload,
    require_ready,
    upload_duration_seconds,
)
from .settings import HeadId, Mode
from .state import EngineConfig
from .vocab import head_alphabet, split_representable

router = APIRouter()
admin = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
STARTED_AT = time.monotonic()


class DeployRequest(BaseModel):
    variant: HeadId
    punctuation: Mode | None = None
    itn: Mode | None = None
    vad: bool | None = None
    pool_size: int | None = Field(default=None, ge=1, le=8)
    hotwords_boost: float | None = Field(default=None, ge=0.0, le=50.0)
    hotwords_default: bool | None = None

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
        "engine_detail": supervisor.detail,
        "variant": supervisor.current.variant if supervisor.current else None,
    }


def _status_payload(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    settings = request.app.state.settings
    current = supervisor.current
    return {
        "status": supervisor.status,
        "detail": supervisor.detail,
        "download_percent": supervisor.download_percent,
        "engine": {
            "variant": current.variant if current else None,
            "punctuation": current.punctuation if current else None,
            "itn": current.itn if current else None,
            "vad": current.vad if current else None,
            "pool_size": current.pool_size if current else None,
            "running": supervisor.process.is_running,
        },
        "defaults": supervisor.default_config().to_dict(),
        "metrics": request.app.state.metrics.snapshot(),
        "api_key_set": bool(settings.api_key),
        "glossary_count": supervisor.glossary_count,
        "restart_count": supervisor.restart_count,
        "uptime_seconds": int(time.monotonic() - STARTED_AT),
        "max_upload_mb": settings.max_upload_mb,
    }


@admin.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _status_payload(request)


@admin.get("/models")
async def models(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    current = request.app.state.supervisor.current
    return {
        "heads": [
            {
                "id": head.id,
                "title": head.title,
                "subtitle": head.subtitle,
                "languages": list(head.languages),
                "native_punctuation": head.native_punctuation,
                "size_mb": head.size_mb,
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
    # Keep a reference so the task is not garbage collected mid-flight.
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
    return {"count": supervisor.glossary_count, "applied": applied}


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
            async for event in subscription:
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
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT) as client:
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


def _engine_error(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"message": response.text[:500], "code": "engine_error"}}
    if isinstance(body, dict) and "error" in body:
        return body
    return {"error": {"message": str(body)[:500], "code": "engine_error"}}


router.include_router(admin)
