"""Bearer token check.

Off by default: with no `API_KEY` in `.env` the service is open (the UI shows a
warning banner). With a key set, both `/v1/*` and `/api/*` require it; `/health`
never does, so Docker's healthcheck keeps working.
"""

from hmac import compare_digest

from fastapi import Request

from .errors import ApiError


_QUERY_KEY_PATHS = frozenset({"/api/events", "/api/docs", "/api/openapi.json"})


async def require_api_key(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.api_key:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    presented = token.strip() if scheme.lower() == "bearer" else ""
    if not presented and request.method == "GET" and request.url.path in _QUERY_KEY_PATHS:
        # Совместимость с EventSource и загрузкой Swagger в браузере.
        presented = request.query_params.get("api_key", "")
    if not compare_digest(presented.encode("utf-8"), settings.api_key.encode("utf-8")):
        raise ApiError(
            401,
            "Неверный или отсутствующий API-ключ. Передайте заголовок "
            "Authorization: Bearer <ключ из .env>.",
            "invalid_api_key",
            type_="authentication_error",
        )
