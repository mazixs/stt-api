"""Application wiring.

Run with: `uvicorn console.main:create_app --factory --host 0.0.0.0 --port 8080`
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import proxy
from .api import router as api_router
from .errors import ApiError, api_error_handler
from .events import EventBus
from .metrics import Metrics
from .settings import Settings, get_settings
from .supervisor import Supervisor

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    bus = EventBus(logger=logging.getLogger("console"))
    metrics = Metrics(settings.metrics_path)
    supervisor = Supervisor(settings, bus)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        restore = asyncio.create_task(supervisor.restore_on_boot())
        try:
            yield
        finally:
            restore.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restore
            await supervisor.shutdown()

    app = FastAPI(
        title="GigaAM STT Console",
        summary="Веб-консоль и OpenAI-совместимый API поверх движка GigaSTT",
        lifespan=lifespan,
        # Штатные маршруты документации выключены: свои, на `/api/docs` и
        # `/api/openapi.json`, живут в `console.api` под проверкой ключа — схема
        # закрывается вместе с сервисом, а не остаётся открытой сама по себе.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.bus = bus
    app.state.metrics = metrics
    app.state.supervisor = supervisor

    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(api_router)
    app.include_router(proxy.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        index_html = _versioned_index()

        @app.get("/", include_in_schema=False)
        async def index() -> HTMLResponse:
            # Страницу браузер обязан перепроверять каждый раз, а скрипт и стили
            # помечены отпечатком содержимого. Без этой пары после выкладки браузер
            # показывает старую разметку со свежим скриптом: скрипт ищет кнопку,
            # которой в кэшированной странице ещё нет, спотыкается на первой же — и
            # консоль остаётся пустой, хотя сервис исправен.
            return HTMLResponse(
                index_html, headers={"Cache-Control": "no-cache, must-revalidate"}
            )

    return app


def _versioned_index() -> str:
    """Разметка, в которой ссылки на ассеты помечены отпечатком их содержимого."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        asset = STATIC_DIR / name
        if not asset.is_file():
            continue
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:8]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={digest}")
    return html
