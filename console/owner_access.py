"""Граница публичного профиля: реальный peer → доверенный HTTPS → cookie либо Bearer."""

import hmac
import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from .errors import ApiError, api_error_handler
from .owner_store import ABSOLUTE_SECONDS, limited
from .settings import Settings

COOKIE = "__Host-kamerton_session"
SSE_CHECK_SECONDS = 5
JSON_LIMIT = 1024 * 1024
LOGIN_JSON_LIMIT = 8192
PUBLIC_GET = {"/login", "/static/style.css", "/static/i18n.js", "/static/login.js"}
CLIENT_ROUTES = {("GET", "/v1/models"), ("POST", "/v1/audio/transcriptions"), ("POST", "/v1/audio/translations")}
SAFE = {"GET", "HEAD", "OPTIONS"}
router = APIRouter()


def exact_ip(value: str):
    if "%" in value:
        raise ValueError
    return ipaddress.ip_address(value)


def public_network(settings: Settings):
    try:
        proxy = exact_ip(settings.trusted_proxy_ip)
    except ValueError:
        raise ValueError("TRUSTED_PROXY_IP должен содержать один IP-адрес.") from None
    try:
        if not exact_ip(settings.engine_host).is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError("ENGINE_HOST в режиме владельца должен быть loopback IP.") from None
    try:
        origin = settings.public_origin
        parts = urlsplit(origin)
        name = parts.hostname or ""
        if ":" in name:
            address = "[" + str(exact_ip(name)) + "]"
        else:
            if (len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                           for label in name.split("."))):
                raise ValueError
            address = name
        if parts.port is not None:
            if parts.port in (0, 80, 443):
                raise ValueError
            address += f":{parts.port}"
        if origin != "https://" + address:
            raise ValueError
    except ValueError:
        raise ValueError("PUBLIC_ORIGIN должен быть точным HTTPS-origin без пути и параметров. "
                         "Порт :443 указывать не нужно, порт :80 запрещён.") from None
    return proxy, address


def single(request, key):
    values = request.headers.getlist(key)
    return values[0] if len(values) == 1 else None


def check_origin(request):
    if single(request, "origin") != request.app.state.settings.public_origin:
        raise ApiError(403, "Недопустимый источник запроса.", "invalid_origin")


async def live_session(request, *, touch=False):
    token = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    return await run_in_threadpool(request.app.state.owner_store.session, token, touch)


async def bounded_body(request, limit):
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > limit:
            raise ApiError(413, "Слишком большое тело запроса.", "json_too_large")
        chunks.extend(chunk)
    return bytes(chunks)


class OwnerBoundary:
    def __init__(self, app, network):
        self.app = app
        self.proxy, self.host = network

    async def authorize(self, request):
        method, path = request.method, request.scope["path"]
        try:
            peer = exact_ip(request.client.host) if request.client else None
        except ValueError:
            peer = None
        if path == "/health" and method == "GET":
            if peer and peer.is_loopback:
                return
            raise ApiError(403, "Проверка доступна только из контейнера.", "internal_only")
        if peer != self.proxy or single(request, "host") != self.host or single(request, "x-forwarded-proto") != "https":
            raise ApiError(403, "Требуется доверенный HTTPS-прокси и точный Host.", "untrusted_request")
        if "api_key" in request.query_params:
            raise ApiError(400, "Передача ключа в URL запрещена.", "query_key_forbidden")
        if path == "/v1" or path.startswith("/v1/"):
            if (method, path) not in CLIENT_ROUTES:
                raise ApiError(404, "Маршрут не найден.", "not_found")
            if method not in SAFE and request.headers.getlist("origin"):
                check_origin(request)
            auth = single(request, "authorization") or ""
            scheme, _, key = auth.partition(" ")
            if not (scheme.lower() == "bearer" and key and await run_in_threadpool(request.app.state.owner_store.accepts_key, key)):
                raise ApiError(401, "Требуется действующий Bearer-ключ.", "invalid_api_key", "authentication_error")
            request.state.owner_subject = "client"
            return
        if path == "/auth/login" and method == "POST":
            check_origin(request)
            if single(request, "x-kamerton-login") != "1":
                raise ApiError(403, "Не задан заголовок входа.", "invalid_login_request")
            return
        if method == "GET" and path in PUBLIC_GET:
            return
        # SSE и фоновое чтение состояния интерфейсом не являются активностью владельца.
        passive = path in {"/api/events", "/auth/session", "/api/status", "/api/journal", "/api/models", "/api/glossary"}
        csrf = await live_session(request, touch=not passive and method in SAFE)
        request.state.owner_csrf = csrf
        if path == "/auth/session" and method == "GET":
            return
        if not csrf:
            if method == "GET" and path == "/":
                return RedirectResponse("/login", status_code=303)
            raise ApiError(401, "Войдите в консоль.", "session_required", "authentication_error")
        if method not in SAFE:
            check_origin(request)
            supplied = (single(request, "x-csrf-token") or "").encode()
            if not hmac.compare_digest(csrf.encode(), supplied):
                raise ApiError(403, "Неверный токен запроса.", "invalid_csrf")
            if not await live_session(request, touch=True):
                raise ApiError(401, "Войдите в консоль.", "session_required", "authentication_error")

        request.state.owner_subject = "owner"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                headers["referrer-policy"] = "no-referrer"
            await send(message)

        body = None
        try:
            response = await self.authorize(request)
            path = scope["path"]
            if (response is None and request.method not in SAFE and not path.startswith("/v1/")
                    and path not in {"/api/test", "/api/compare"}):
                body = await bounded_body(request, LOGIN_JSON_LIMIT if path in {"/auth/login", "/api/keys"} else JSON_LIMIT)
        except ApiError as exc:
            response = await api_error_handler(request, exc)
        if response is not None:
            return await response(scope, receive, private_send)

        async def replay():
            nonlocal body
            if body is None:
                return await receive()
            chunk, body = body, None
            return {"type": "http.request", "body": chunk, "more_body": False}

        return await self.app(scope, replay, private_send)


async def json_fields(request, fields):
    if (single(request, "content-type") or "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "Требуется application/json.", "invalid_content_type")
    try:
        data = await request.json()
        if not isinstance(data, dict) or data.keys() != fields.keys():
            raise ValueError
        if not all(isinstance(data[k], str) and 0 < len(data[k].encode()) <= limit for k, limit in fields.items()):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(400, "Неверные поля JSON-запроса.", "invalid_json") from None


def session_result(csrf):
    result = {"mode": "owner", "authenticated": bool(csrf)}
    if csrf:
        result["csrf_token"] = csrf
    return result


@router.post("/auth/login")
async def login(request: Request):
    data = await json_fields(request, {"username": 256, "password": 1024})
    try:
        client = str(exact_ip(single(request, "x-kamerton-client-ip") or ""))
    except ValueError:
        client = "unknown"
    slots = request.app.state.login_slots
    if slots.locked():
        raise limited()
    async with slots:
        result = await run_in_threadpool(request.app.state.owner_store.login, client, data["username"], data["password"])
    if not result:
        raise ApiError(401, "Неверный логин или пароль.", "invalid_credentials", "authentication_error")
    token, csrf = result
    response = JSONResponse(session_result(csrf))
    response.set_cookie(COOKIE, token, max_age=ABSOLUTE_SECONDS, secure=True, httponly=True, samesite="strict")
    return response


@router.post("/auth/logout", status_code=204)
async def logout(request: Request):
    await run_in_threadpool(request.app.state.owner_store.logout, request.cookies[COOKIE])
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
    return response


@router.get("/api/keys")
async def keys(request: Request):
    return {"keys": await run_in_threadpool(request.app.state.owner_store.keys)}


@router.post("/api/keys", status_code=201)
async def create_key(request: Request):
    data = await json_fields(request, {"name": 256})
    name = data["name"].strip()
    if not name or not name.isprintable():
        raise ApiError(400, "Укажите название ключа.", "invalid_key_name")
    return await run_in_threadpool(request.app.state.owner_store.create_key, name)


@router.post("/api/keys/{key_id}/revoke", status_code=204)
async def revoke_key(key_id: str, request: Request):
    await run_in_threadpool(request.app.state.owner_store.revoke, key_id)
    return Response(status_code=204)
