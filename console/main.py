"""Application wiring.

Run with: `uvicorn console.main:create_app --factory --host 0.0.0.0 --port 8080`
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, owner_access, proxy
from .api import router as api_router
from .compare import clean_compare_temp_root, router as compare_router
from .errors import ApiError, api_error_handler
from .events import EventBus
from .metrics import Metrics
from .owner_store import OwnerStore
from .settings import Settings, get_settings
from .supervisor import Supervisor

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    network = owner_access.public_network(settings) if settings.owner_access else None
    store = OwnerStore(settings.data_dir)
    if not settings.owner_access:
        store.guard_local(rollback=settings.allow_owner_local_rollback, key=settings.api_key)
    owner_store = store if settings.owner_access else None
    bus = EventBus(logger=logging.getLogger("console"))
    metrics = Metrics(settings.metrics_path)
    supervisor = Supervisor(settings, bus)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owner_store is not None:
            await run_in_threadpool(owner_store.check)
        try:
            clean_compare_temp_root(settings)
        except OSError:
            settings.enable_model_compare = False
            logging.getLogger("console").warning(
                "Не удалось очистить временный каталог; сравнение моделей отключено."
            )
        restore = asyncio.create_task(supervisor.restore_on_boot())
        try:
            yield
        finally:
            pending = [restore, *app.state.deploy_tasks]
            for task in pending:
                task.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
                if not restore.cancelled():
                    restore.result()
            finally:
                await supervisor.shutdown()

    app = FastAPI(
        title="GigaAM STT Console",
        summary="Веб-консоль и OpenAI-совместимый API поверх движка GigaSTT",
        version=__version__,
        lifespan=lifespan,
        # В публичном профиле ASGI намеренно видит HTTP до прокси: не строим
        # абсолютные редиректы с лишним слешем на внутренней схеме.
        redirect_slashes=not settings.owner_access,
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

    app.state.compare_lock = asyncio.Lock()
    app.state.deploy_tasks = set()

    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(api_router)
    app.include_router(compare_router)
    app.include_router(proxy.router)
    if owner_store is not None:
        app.state.owner_store = owner_store
        app.state.login_slots = asyncio.Semaphore(2)
        app.add_middleware(owner_access.OwnerBoundary, network=network)
        app.include_router(owner_access.router)

        @app.exception_handler(RequestValidationError)
        async def invalid_request(request: Request, _exc):
            # Стандартный 422 содержит input, иногда со всем присланным JSON.
            return await api_error_handler(request, ApiError(422, "Проверьте поля запроса.", "invalid_request"))

        login_html = _versioned_index("login.html")

        @app.get("/login", include_in_schema=False)
        async def login_page():
            return HTMLResponse(login_html, headers={"Cache-Control": "no-store"})
    else:
        app.include_router(proxy.native_router)

    @app.get("/auth/session", include_in_schema=False)
    async def auth_session(request: Request):
        result = (owner_access.session_result(request.state.owner_csrf) if owner_store is not None
                  else {"mode": "local"})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

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


def _versioned_index(filename: str = "index.html") -> str:
    """Разметка, в которой ссылки на ассеты помечены отпечатком их содержимого."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    for name in ("i18n.js", "app.js", "owner.js", "style.css", "login.js"):
        asset = STATIC_DIR / name
        if not asset.is_file():
            continue
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:8]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={digest}")
    return html
