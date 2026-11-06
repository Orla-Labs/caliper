"""Export from the UI or a script: preview the consensus, download, or write to a destination.

* ``GET  /api/export/summary``   what an export would contain and what it leaves out;
* ``GET  /api/export/download``  the file itself (attachment);
* ``POST /api/export``           write to a destination: a file inside the project
  folder, an ``s3://`` URL, or a database table (via dev 1's ``sinks``), or one of
  the project's saved ``exports`` targets.
"""

from __future__ import annotations

import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .errors import ApiError
from .export import (
    FORMAT_HELP,
    ConflictPolicy,
    ExportError,
    ExportFormat,
    RowsMode,
    csv_text,
    session_rows,
    summary,
    write_local,
    write_via_sinks,
)
from .routes_label import get_session
from .state import Session

router = APIRouter(tags=["export"])
S = Annotated[Session, Depends(get_session)]

EXT = {
    "golden": "jsonl",
    "rollouts": "jsonl",
    "records": "jsonl",
    "jsonl": "jsonl",
    "csv": "csv",
    "parquet": "parquet",
}
MEDIA = {
    "jsonl": "application/x-ndjson",
    "csv": "text/csv; charset=utf-8",
    "parquet": "application/vnd.apache.parquet",
}


class ExportIn(BaseModel):
    format: ExportFormat
    conflicts: ConflictPolicy = Field(
        default=ConflictPolicy.EXCLUDE,
        description="double-labelled items that disagree and have no adjudication: "
        "`exclude` leaves them out, `majority` takes a strict majority (ties left out)",
    )
    rows: RowsMode = Field(
        default=RowsMode.ITEMS, description="label table: one row per item or per label"
    )
    include_unlabelled: bool = Field(
        default=False, description="also write items without a final label (marked by status)"
    )
    destination: dict[str, Any] | None = Field(
        default=None,
        description="a SourceConfig: {kind: file, path} (inside the project), "
        "{kind: s3, path: s3://bucket/key.parquet, credentials}, "
        "{kind: postgres|mysql|sqlite, table, credentials}",
    )
    target: str | None = Field(
        default=None, description="name of a saved target in the project's `exports`"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "labels"


def _name(session: Session, request: Request) -> str:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is not None:
        return ctx.config.name
    return session.config.title if session.config else "labels"


def _root(request: Request) -> Path:
    ctx = getattr(request.app.state, "ctx", None)
    return Path(ctx.project_dir) if ctx is not None else Path.cwd()


def _rows(session: Session, body: ExportIn) -> tuple[Any, Any]:
    try:
        return session_rows(
            session,
            body.format,
            body.conflicts,
            rows=body.rows,
            include_unlabelled=body.include_unlabelled,
        )
    except ExportError as exc:
        raise ApiError(400, str(exc), exc.hint) from None


DB_KINDS = ("postgres", "mysql", "sqlite")


def _db_source(ctx: Any) -> dict[str, Any] | None:
    """The project's source database (references only, never secrets) for table write-back."""
    if ctx is None or ctx.config.source.kind.value not in DB_KINDS:
        return None
    src = ctx.config.source
    out: dict[str, Any] = {
        "kind": src.kind.value,
        "credentials": src.credentials.model_dump(mode="json"),
    }
    if src.kind.value == "sqlite":
        out["path"] = src.path
    return out


@router.get("/api/export/formats", summary="Export formats and the project's saved targets")
def formats(request: Request, session: S) -> dict[str, Any]:
    ctx = getattr(request.app.state, "ctx", None)
    legacy_kind = session.dataset.kind.value if session.dataset is not None else None
    return {
        "formats": [{"name": k, "help": v} for k, v in FORMAT_HELP.items()],
        "targets": [t.model_dump(mode="json") for t in ctx.config.exports] if ctx else [],
        "data_kind": legacy_kind,
        "label_type": session.rules.type.value,
        "source": _db_source(ctx),
        "double_label": session.needed,
    }


@router.get("/api/export/summary", summary="What an export would hold and what it leaves out")
def export_summary(
    session: S, conflicts: ConflictPolicy = ConflictPolicy.EXCLUDE
) -> dict[str, Any]:
    from .export import consensus, session_consensus

    if session.dataset is not None:
        cons = consensus(session.dataset, session.config, session.records, conflicts)
    else:
        cons = session_consensus(session, conflicts)
    return summary(cons, session, conflicts.value)


@router.get(
    "/api/export/download",
    summary="Download an export file",
    response_class=Response,
)
def download(
    session: S,
    request: Request,
    format: ExportFormat,
    conflicts: ConflictPolicy = ConflictPolicy.EXCLUDE,
    rows: RowsMode = RowsMode.ITEMS,
    include_unlabelled: bool = False,
) -> Response:
    if format is ExportFormat.TABLE:
        raise ApiError(
            400, "a table export has no file to download", "POST /api/export with a destination"
        )
    body = ExportIn(
        format=format, conflicts=conflicts, rows=rows, include_unlabelled=include_unlabelled
    )
    data, _ = _rows(session, body)
    ext = EXT[format.value]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    fname = f"{_slug(_name(session, request))}-{format.value}-{stamp}.{ext}"
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store",
    }
    try:
        if format is ExportFormat.CSV:
            text, _n = csv_text(data)
            return Response(text, media_type=MEDIA["csv"], headers=headers)
        if format is ExportFormat.PARQUET:
            tmp = Path(tempfile.mkdtemp(prefix="caliper-label-export-")) / fname
            write_local(data, format, tmp)
            return FileResponse(
                tmp,
                media_type=MEDIA["parquet"],
                headers=headers,
                background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)),
            )
        import json

        text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in data)
        return Response(text, media_type=MEDIA["jsonl"], headers=headers)
    except ExportError as exc:
        raise ApiError(400, str(exc), exc.hint) from None


def _safe_file(root: Path, path: str) -> Path:
    if "\x00" in path:
        raise ApiError(400, "bad path")
    root = root.resolve()
    p = (root / path).resolve()
    if not p.is_relative_to(root):
        raise ApiError(
            403,
            "file exports are written inside the project folder",
            "use a relative path such as exports/labels.csv, or export to S3 / a database",
        )
    return p


@router.post(
    "/api/export",
    summary="Write an export to a file, S3 or a database table",
    description="Local files must be inside the project folder. S3 and databases go through the "
    "same connectors and credential rules as data sources (credentials are env-var references).",
)
def post_export(session: S, request: Request, body: ExportIn) -> dict[str, Any]:
    ctx = getattr(request.app.state, "ctx", None)
    dest = body.destination
    if body.target:
        if ctx is None:
            raise ApiError(400, "saved targets need a project", None)
        tgt = next((t for t in ctx.config.exports if t.name == body.target), None)
        if tgt is None:
            raise ApiError(
                404, f"no saved export target '{body.target}'", "see GET /api/export/formats"
            )
        dest = tgt.destination.model_dump(mode="json", exclude_none=True)
        if body.format is None:  # pragma: no cover - format is required by the model
            body.format = ExportFormat(tgt.format)
    data, cons = _rows(session, body)
    kind = (dest or {}).get("kind", "file")
    try:
        if kind == "file":
            ext = EXT.get(body.format.value)
            if ext is None:
                raise ApiError(
                    400, "a table export needs a database destination", "set destination.kind"
                )
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            rel = (dest or {}).get(
                "path"
            ) or f"exports/{_slug(_name(session, request))}-{body.format.value}-{stamp}.{ext}"
            path = _safe_file(_root(request), rel)
            n = write_local(data, body.format, path)
            where = str(path)
        else:
            fmt = "table" if kind in DB_KINDS else body.format.value
            n = write_via_sinks(data, dest, fmt, project_dir=_root(request))
            where = dest.get("table") or dest.get("path") or kind
    except ExportError as exc:
        raise ApiError(400, str(exc), exc.hint) from None
    return {
        "written": n,
        "destination": where,
        "format": body.format.value,
        "summary": summary(cons, session, body.conflicts.value),
    }
