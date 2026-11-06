"""The one error shape every `/api` route returns: ``{"detail": str, "hint": str | None}``.

Raise :class:`ApiError` (or any ``HTTPException``) from a route; the handlers that
``app.create_app`` installs turn it, request validation errors and
``ConnectorError`` into that JSON shape. ``hint`` is the fix in plain words.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(HTTPException):
    def __init__(self, status: int, detail: str, hint: str | None = None) -> None:
        super().__init__(status_code=status, detail=detail)
        self.hint = hint


def body(detail: str, hint: str | None = None) -> dict[str, Any]:
    return {"detail": detail, "hint": hint}


NO_PROJECT_HINT = "open a project from the start screen, or run `caliper label PROJECT_DIR`"


def _validation_text(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        hint = getattr(exc, "hint", None)
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(body(detail, hint), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            body(_validation_text(exc), "check the request against the API docs at /docs"),
            status_code=422,
        )

    try:  # dev 1's connector errors carry their own hint
        from .connectors import ConnectorError  # type: ignore[attr-defined]
    except ImportError:
        return

    @app.exception_handler(ConnectorError)
    async def _connector(request: Request, exc: Any) -> JSONResponse:
        return JSONResponse(
            body(str(getattr(exc, "message", exc)), getattr(exc, "hint", None)), status_code=502
        )
