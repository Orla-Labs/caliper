"""Data routes (dev 1): test a connection, preview a source, snapshot/refresh, serve media.

Every error is JSON ``{"detail": str, "hint": str | null}`` (CONTRACT). Routes that act on the
open project read ``request.app.state.ctx`` and answer 409 when no project is open. The
``/api/source/test|preview|credentials`` routes also take a ``SourceConfig`` in the body so the
setup wizard can use them before a project exists; relative paths then resolve from
``project_dir`` (body) or the open project's directory.
"""

from __future__ import annotations

import logging
import threading
import time
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
from .connectors import media as media_mod
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


_log = logging.getLogger("uvicorn.error")  # the server's console channel (runs at WARNING)


class _MediaLog:
    """One console line per distinct media failure, then a count, not one per image."""

    EVERY = 60.0  # seconds between repeats of the same cause

    def __init__(self) -> None:
        self.seen: dict[str, list[float]] = {}  # detail -> [last_logged, count_since]
        self.lock = threading.Lock()

    def failed(self, item_id: str, field: str, detail: str, hint: str | None) -> None:
        now = time.monotonic()
        with self.lock:
            rec = self.seen.get(detail)
            if rec is not None and now - rec[0] < self.EVERY:
                rec[1] += 1
                return
            more = int(rec[1]) if rec else 0
            self.seen[detail] = [now, 0]
        tail = f" ({more} more like this since the last line)" if more else ""
        _log.warning(
            "caliper label: image for item %s field %s not served: %s%s%s",
            item_id,
            field,
            detail,
            f". Fix: {hint}" if hint else "",
            tail,
        )


media_log = _MediaLog()


def _image_field(ctx: Any, field: str) -> Any:
    fields = ctx.config.card.fields
    spec = None
    if field.isdigit() and int(field) < len(fields):
        spec = fields[int(field)]
    else:
        spec = next((f for f in fields if f.column == field), None)
    if spec is None or spec.type is not DisplayType.IMAGE:
        raise ApiError(404, f"no image field '{field}'", "media is served for image card fields")
    return spec


def _media_value(spec: Any, fields: dict[str, Any]) -> Any:
    if spec.template is not None:
        return render_template(spec.template, fields)
    value = fields.get(spec.column) if spec.column in fields else None
    if value is None and spec.column:
        value = get_path(fields, spec.column)
    return value


def _media_status(message: str) -> int:
    low = message.lower()
    if "credentials" in low or "sign" in low:
        return 502
    if "outside" in low or "type" in low:
        return 403
    return 404


def _checker(conn: Any, source: SourceConfig | None) -> Callable[[Any], dict[str, Any]]:
    return lambda value: media_mod.check_ref(value, conn.resolve_media, source)


@router.get("/api/media/{item_id}/{field}")
def media(request: Request, item_id: str, field: str) -> Response:
    """Image for card field ``field`` (index or column name) of an item.

    The client never sends a path: the server reads the value from the snapshot and resolves
    it through the connector (confined local file, or a 302 to a presigned/HTTP URL). A
    failure answers ``{detail, hint}`` and is logged once per cause on the server console;
    ``GET /api/media/{item_id}/{field}/check`` explains it as JSON for the card.
    """
    ctx = _ctx(request)
    item = ctx.store.item(item_id)
    if item is None:
        raise ApiError(404, f"unknown item '{item_id}'")
    spec = _image_field(ctx, field)
    value = _media_value(spec, item.fields)
    if not value or not isinstance(value, str):
        raise ApiError(404, "this item has no image", None)
    try:
        ref = ctx.connector.resolve_media(value)
    except ConnectorError as exc:
        detail, hint = redact(exc.message, ctx.config.source), exc.hint
        media_log.failed(item_id, field, detail, hint)
        raise ApiError(_media_status(exc.message), detail, hint) from None
    if ref.kind == "url" and ref.url:
        return RedirectResponse(ref.url, status_code=302, headers={"Cache-Control": "no-store"})
    if not ref.path:
        raise ApiError(404, "image not found")
    return FileResponse(ref.path, media_type=ref.mime, headers=_MEDIA_HEADERS)


@router.get("/api/media/{item_id}/{field}/check")
def media_check_item(request: Request, item_id: str, field: str) -> dict[str, Any]:
    """Why an item's image does (not) load: always 200 with ``{ok, kind, status, detail, hint}``.

    Signed S3 links are fetched once (first byte) by the server, so a missing object, a refused
    signature or a wrong endpoint shows up here; plain http(s) links are not fetched (``ok``
    is null). The signed URL itself is never returned.
    """
    ctx = _ctx(request)
    item = ctx.store.item(item_id)
    if item is None:
        raise ApiError(404, f"unknown item '{item_id}'")
    spec = _image_field(ctx, field)
    value = _media_value(spec, item.fields)
    out = _checker(ctx.connector, ctx.config.source)(value)
    if out["ok"] is False and out["detail"]:
        media_log.failed(item_id, field, out["detail"], out["hint"])
    return {"item_id": item_id, "field": field, "value": value, **out}


class MediaCheckIn(SourceIn):
    column: str | None = None  # a source column holding image values ...
    template: str | None = None  # ... or a card template such as "cards/{id}.png"
    n: int = Field(default=5, ge=1, le=20)  # how many non-empty values to try


@router.post("/api/source/media-check")
def media_check(request: Request, body: MediaCheckIn | None = None) -> dict[str, Any]:
    """Try the first ``n`` image values of a column, as the labeller would load them.

    For the setup wizard (body ``source``) or the open project (no source: every image card
    field). Always 200: ``{ok, checked, failed, results: [{column, value, ok, kind, status,
    detail, hint}], detail, hint}``; the top-level ``detail``/``hint`` repeat the first failure.
    ``ok`` is null when nothing could be checked (only web links, or no values).
    """
    body = body or MediaCheckIn()
    source, pdir, conn, owned = _source(request, body)
    specs: list[tuple[str, Any]] = []
    if body.column or body.template:
        from .project import CardField

        spec = CardField(column=body.column, template=body.template, type=DisplayType.IMAGE)
        specs.append((body.column or body.template or "", spec))
    elif body.source is None:
        ctx = _ctx(request)
        specs = [
            (f.column or f.template or str(i), f)
            for i, f in enumerate(ctx.config.card.fields)
            if f.type is DisplayType.IMAGE
        ]
    else:
        raise ApiError(400, "which column holds the images?", "send `column` (or `template`)")
    results: list[dict[str, Any]] = []
    try:
        conn = conn or open_connector(source, pdir)
        try:
            check = _checker(conn, source)
            rows = conn.preview(max(body.n * 4, 20)) if specs else []
            for name, spec in specs:
                values = [v for r in rows if (v := _media_value(spec, r)) not in (None, "")]
                for v in values[: body.n]:
                    results.append({"column": name, "value": v, **check(v)})
        finally:
            if owned and hasattr(conn, "close"):
                conn.close()
    except ConnectorError as exc:
        return {
            "ok": False,
            "checked": 0,
            "failed": 0,
            "results": [],
            "detail": redact(exc.message, source),
            "hint": exc.hint and redact(exc.hint, source),
        }
    failed = [r for r in results if r["ok"] is False]
    checked = [r for r in results if r["ok"] is not None]
    first = failed[0] if failed else None
    if first is not None:
        _log.warning(
            "caliper label: media check: %d of %d image(s) failed: %s%s",
            len(failed),
            len(results),
            first["detail"],
            f". Fix: {first['hint']}" if first["hint"] else "",
        )
    return {
        "ok": (not failed) if checked else None,
        "checked": len(results),
        "failed": len(failed),
        "results": results,
        "detail": first["detail"] if first else None,
        "hint": first["hint"] if first else None,
    }
