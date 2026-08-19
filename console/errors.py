"""OpenAI-shaped error responses.

Clients written against the OpenAI SDK expect `{"error": {...}}`, so every failure
the console produces — auth, size limits, engine not ready — uses this envelope
instead of FastAPI's default `{"detail": ...}`.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        type_: str = "invalid_request_error",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.type = type_
        self.headers = headers or {}


def openai_error(
    status: int,
    message: str,
    code: str,
    type_: str = "invalid_request_error",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code, "param": None}},
        headers=headers,
    )


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return openai_error(exc.status, exc.message, exc.code, exc.type, exc.headers)
