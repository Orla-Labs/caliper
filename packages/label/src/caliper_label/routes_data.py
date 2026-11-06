"""Data routes (dev 1): test a connection, preview a source, snapshot/refresh, serve media.

Every error is JSON ``{"detail": str, "hint": str | null}`` (CONTRACT). Routes that act on the
open project read ``request.app.state.ctx`` and answer 409 when no project is open. The
``/api/source/test|preview|credentials`` routes also take a ``SourceConfig`` in the body so the
setup wizard can use them before a project exists; relative paths then resolve from
``project_dir`` (body) or the open project's directory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from . import snapshot
from .config import get_path, render_template
from .connectors import ConnectorError, available_connectors, open_connector
from .credentials import credential_status, redact
from .project import DisplayType, SourceConfig

PREVIEW_ROWS = 50


class ApiError(Exception):
    def __init__(self, status: int, detail: str, hint: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.hint = status, detail, hint


class DataRoute(APIRoute):
    """Turns ConnectorError/ApiError into the contract's error shape inside this router."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except ApiError as exc:
                return JSONResponse({"detail": exc.detail, "hint": exc.hint}, exc.status)
            except ConnectorError as exc:
                return JSONResponse(
                    {"detail": redact(exc.message), "hint": exc.hint and redact(exc.hint)}, 400
                )

        return wrapped


router = APIRouter(route_class=DataRoute, tags=["data"])


class SourceIn(BaseModel):
    source: SourceConfig | None = None  # None -> the open project's source
    project_dir: str | None = None
    n: int = Field(default=PREVIEW_ROWS, ge=0, le=500)


class ApplyIn(BaseModel):
    diff_id: str
    note: str | None = None


def _ctx(request: Request, required: bool = True) -> Any:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None and required:
        raise ApiError(409, "no project open", "open or create a project first")
    return ctx


def _source(request: Request, body: SourceIn | None):
    """(source, project_dir, connector, owned) for a body source or the open project."""
    ctx = _ctx(request, required=False)
    if body is not None and body.source is not None:
        pdir = Path(body.project_dir) if body.project_dir else (ctx.project_dir if ctx else None)
        pdir = pdir or Path.cwd()
        return body.source, pdir, None, True
    if ctx is None:
        raise ApiError(409, "no project open", "send a source, or open a project first")
    return ctx.config.source, Path(ctx.project_dir), ctx.connector, False


def _checks(source: SourceConfig) -> list[dict[str, Any]]:
    return [c.model_dump() for c in credential_status(source)]


@router.get("/api/source/connectors")
def connectors() -> dict[str, Any]:
    """Connector kinds available here (built-ins, registered, installed plugins)."""
    return {"connectors": available_connectors()}


@router.post("/api/source/credentials")
def credentials(request: Request, body: SourceIn | None = None) -> dict[str, Any]:
    source, *_ = _source(request, body)
    return {"credentials": _checks(source)}


@router.post("/api/source/test")
def test_connection(request: Request, body: SourceIn | None = None) -> dict[str, Any]:
    """Always 200: ``ok`` plus credential status, row count or the error with its fix."""
    source, pdir, conn, owned = _source(request, body)
    checks = _checks(source)
    try:
        conn = conn or open_connector(source, pdir)
        try:
            n = conn.count()
            cols = [c.model_dump() for c in conn.schema()]
        finally:
            if owned and hasattr(conn, "close"):
                conn.close()
    except ConnectorError as exc:
        return {
            "ok": False,
            "credentials": checks,
            "detail": redact(exc.message, source),
            "hint": exc.hint and redact(exc.hint, source),
        }
    return {"ok": True, "credentials": checks, "count": n, "columns": cols}


def _preview(request: Request, body: SourceIn | None) -> dict[str, Any]:
    source, pdir, conn, owned = _source(request, body)
    n = body.n if body is not None else PREVIEW_ROWS
    conn = conn or open_connector(source, pdir)
    try:
        columns = conn.schema()
        rows = conn.preview(n)
        count = conn.count()
    finally:
        if owned and hasattr(conn, "close"):
            conn.close()
    return {
        "columns": [c.model_dump() for c in columns],
        "rows": rows,
        "count": count,
        "credentials": _checks(source),
    }


@router.post("/api/source/preview")
def preview(request: Request, body: SourceIn | None = None) -> dict[str, Any]:
    """Schema with detected display types, the first ``n`` rows (default 50), and the count."""
    return _preview(request, body)


@router.get("/api/source/preview")
def preview_project(request: Request, n: int = PREVIEW_ROWS) -> dict[str, Any]:
    return _preview(request, SourceIn(n=max(0, min(n, 500))))


@router.post("/api/source/snapshot")
def take_snapshot(request: Request) -> dict[str, Any]:
    """Snapshot the open project's source (no-op when one exists)."""
    return snapshot.take(_ctx(request)).model_dump()


@router.get("/api/source/snapshots")
def snapshots(request: Request, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    ctx = _ctx(request)
    return {
        "snapshots": [s.model_dump() for s in ctx.store.snapshots(offset, min(limit, 500))],
        "items": ctx.store.count(),
        "removed": ctx.store.count(removed_only=True),
    }


@router.post("/api/source/refresh")
def refresh(request: Request) -> dict[str, Any]:
    """Read the source again; returns the diff (new/removed/changed). Apply it separately."""
    return snapshot.diff(_ctx(request)).model_dump()


@router.get("/api/source/refresh/{diff_id}/ids")
def refresh_ids(
    request: Request,
    diff_id: str,
    which: Literal["new", "removed", "changed"] = "new",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    ctx = _ctx(request)
    ids = snapshot.diff_ids(ctx.store, diff_id, which, max(0, offset), max(0, min(limit, 1000)))
    return {"which": which, "offset": offset, "ids": ids}


@router.post("/api/source/refresh/apply")
def refresh_apply(request: Request, body: ApplyIn) -> dict[str, Any]:
    return snapshot.apply(_ctx(request), body.diff_id, note=body.note).model_dump()


# --------------------------------------------------------------------------- media

_MEDIA_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=300",
}


@router.get("/api/media/{item_id}/{field}")
def media(request: Request, item_id: str, field: str) -> Response:
    """Image for card field ``field`` (index or column name) of an item.

    The client never sends a path: the server reads the value from the snapshot and resolves
    it through the connector (confined local file, or a 302 to a presigned/HTTP URL).
    """
    ctx = _ctx(request)
    item = ctx.store.item(item_id)
    if item is None:
        raise ApiError(404, f"unknown item '{item_id}'")
    fields = ctx.config.card.fields
    spec = None
    if field.isdigit() and int(field) < len(fields):
        spec = fields[int(field)]
    else:
        spec = next((f for f in fields if f.column == field), None)
    if spec is None or spec.type is not DisplayType.IMAGE:
        raise ApiError(404, f"no image field '{field}'", "media is served for image card fields")
    if spec.template is not None:
        value = render_template(spec.template, item.fields)
    else:
        value = item.fields.get(spec.column) if spec.column in item.fields else None
        if value is None and spec.column:
            value = get_path(item.fields, spec.column)
    if not value or not isinstance(value, str):
        raise ApiError(404, "this item has no image", None)
    try:
        ref = ctx.connector.resolve_media(value)
    except ConnectorError as exc:
        status = 403 if "outside" in exc.message or "type" in exc.message else 404
        if "credentials" in exc.message.lower() or "sign" in exc.message:
            status = 502
        raise ApiError(status, redact(exc.message), exc.hint) from None
    if ref.kind == "url" and ref.url:
        return RedirectResponse(ref.url, status_code=302, headers={"Cache-Control": "no-store"})
    if not ref.path:
        raise ApiError(404, "image not found")
    return FileResponse(ref.path, media_type=ref.mime, headers=_MEDIA_HEADERS)
