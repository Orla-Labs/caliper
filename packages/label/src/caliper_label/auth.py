"""Team mode: one shared access token (``--auth token``). No SSO/RBAC (phase 2).

* The token is generated at startup (``secrets.token_urlsafe``) unless
  ``CALIPER_LABEL_TOKEN`` is set, and printed once in the startup URL.
* A request is let in when it carries the token as ``Authorization: Bearer <t>``,
  as ``?token=<t>`` (the link people open), or as the ``caliper_label_token``
  cookie. The first time a header or query token checks out, the server sets that
  cookie (HttpOnly, SameSite=Strict) so ``<img>`` tags and downloads, which cannot
  send headers, work too. The UI then drops ``?token=`` from the address bar.
* Comparison is constant-time (``secrets.compare_digest``).
* Protected: every ``/api`` route except ``/api/status`` (which only says whether
  a token is needed). The static UI is public: it holds no data.
* The token is never logged: uvicorn's access log is off, and
  :class:`RedactTokenFilter` scrubs ``token=...`` from any uvicorn log line anyway.
"""

from __future__ import annotations

import logging
import os
import re
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .errors import body

COOKIE = "caliper_label_token"
ENV = "CALIPER_LABEL_TOKEN"
PUBLIC_API = {"/api/status"}
HINT = (
    "open the link printed by `caliper label --auth token` (it ends in ?token=...), "
    "or send the header `Authorization: Bearer <token>`"
)


def make_token() -> str:
    return os.environ.get(ENV) or secrets.token_urlsafe(24)


def same(a: str | None, b: str) -> bool:
    if not a:
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def presented(request: Request) -> tuple[str | None, str]:
    """(token, where): where is header|query|cookie|none."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip(), "header"
    q = request.query_params.get("token")
    if q:
        return q, "query"
    c = request.cookies.get(COOKIE)
    if c:
        return c, "cookie"
    return None, "none"


def is_authed(request: Request) -> bool:
    expected = getattr(request.app.state, "auth_token", None)
    if not expected:
        return True
    tok, _ = presented(request)
    return same(tok, expected)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        tok, where = presented(request)
        ok = same(tok, self.token)
        if not ok and path not in PUBLIC_API:
            detail = "access token required" if tok is None else "wrong access token"
            return JSONResponse(
                body(detail, HINT), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        response = await call_next(request)
        if (
            ok
            and where in ("header", "query")
            and not same(request.cookies.get(COOKIE), self.token)
        ):
            response.set_cookie(
                COOKIE,
                self.token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response


_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact(text: str, token: str | None = None) -> str:
    text = _TOKEN_RE.sub(r"\1[redacted]", text)
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    if token:
        text = text.replace(token, "[redacted]")
    return text


class RedactTokenFilter(logging.Filter):
    """Scrub the token from log records (access lines carry the query string)."""

    def __init__(self, token: str | None = None) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg, self.token)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction(token: str | None) -> None:
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in logger.filters):
            logger.addFilter(RedactTokenFilter(token))
