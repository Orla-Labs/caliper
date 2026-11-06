"""FastAPI app for `caliper label serve`: JSON API + static single-page UI.

Local images are served only from the image root (the data file's directory by
default). The client never sends a path: it asks for ``/api/image/{item}/{n}``
and the server resolves field ``n`` of that item itself, then refuses anything
that resolves outside the root (``..``, absolute paths, symlinks) or is not an
image type.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agreement import agreement
from .config import FieldSpec, FieldType, LabelConfig, get_path, render_template
from .data import Dataset, Item
from .state import LabelError, Session, clean_name

STATIC_DIR = Path(__file__).parent / "static"

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
}


class LabelIn(BaseModel):
    item_id: str
    labeller: str
    decision: str | None = None
    skip: bool = False
    note: str | None = None


class UndoIn(BaseModel):
    labeller: str


def _is_url(v: str) -> bool:
    return v.startswith(("http://", "https://"))


def _field_raw(spec: FieldSpec, row: dict[str, Any]) -> Any:
    if spec.template is not None:
        return render_template(spec.template, row)
    assert spec.key is not None
    return get_path(row, spec.key)


def _display(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, indent=2, sort_keys=True)


def render_card(item: Item, config: LabelConfig) -> list[dict[str, Any]]:
    """The fields of one card as JSON-safe display values (the client escapes them)."""
    out = []
    for n, spec in enumerate(config.fields):
        raw = _field_raw(spec, item.raw)
        f: dict[str, Any] = {"type": spec.type.value, "label": spec.name, "mono": spec.mono}
        if raw is None or raw == "":
            f["value"] = None
        elif spec.type is FieldType.NUMBER:
            if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
                f["value"] = f"{raw:.{spec.decimals}f}" if spec.decimals is not None else str(raw)
            else:
                f["value"] = _display(raw)
        elif spec.type is FieldType.LINK:
            s = str(raw)
            f["value"] = s
            f["href"] = s if _is_url(s) else None
        elif spec.type is FieldType.IMAGE:
            s = str(raw)
            f["value"] = s
            f["src"] = s if _is_url(s) else f"/api/image/{quote(item.id, safe='')}/{n}"
        else:
            f["value"] = _display(raw)
        out.append(f)
    return out


def item_payload(session: Session, item: Item, labeller: str | None) -> dict[str, Any]:
    st = session.state(item.id)
    mine = st.labels.get(labeller or "")
    return {
        "id": item.id,
        "index": item.index,
        "queue": item.queue,
        "category": item.category,
        "fields": render_card(item, session.config),
        "labels_so_far": len(st.labels),
        "labels_needed": session.needed,
        "skipped_before": bool(labeller and labeller in st.skips),
        "my_decision": mine.decision if mine else None,
    }


def resolve_image(item: Item, n: int, config: LabelConfig, root: Path) -> Path:
    """Local image path for field ``n`` of ``item``, or HTTPException. Never leaves ``root``."""
    if n < 0 or n >= len(config.fields) or config.fields[n].type is not FieldType.IMAGE:
        raise HTTPException(404, "no such image field")
    raw = _field_raw(config.fields[n], item.raw)
    if not raw or not isinstance(raw, str) or _is_url(raw):
        raise HTTPException(404, "no local image")
    if "\x00" in raw:
        raise HTTPException(400, "bad path")
    root = root.resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(403, "image path outside the image root")
    if target.suffix.lower() not in IMAGE_TYPES:
        raise HTTPException(403, "not an image file type")
    if not target.is_file():
        raise HTTPException(404, "image not found")
    return target


def create_app(
    session: Session, *, default_labeller: str | None = None, image_root: Path | None = None
) -> FastAPI:
    config = session.config
    dataset: Dataset = session.dataset
    root = image_root or (
        (dataset.path.parent / config.image_root) if config.image_root else dataset.path.parent
    )
    app = FastAPI(title="caliper label", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = session

    def fail(exc: LabelError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {
            "title": config.title,
            "instructions": config.instructions,
            "data_file": dataset.path.name,
            "data_kind": dataset.kind.value,
            "n_items": len(dataset.items),
            "decisions": [
                {
                    "key": d.key,
                    "value": d.value,
                    "label": d.label or d.value,
                    "approved": d.approved,
                }
                for d in config.decisions
            ],
            "keys": config.keys.model_dump(),
            "queues": dataset.queues,
            "double_label": config.double_label,
            "default_labeller": default_labeller,
            "store": session.store.path.name,
        }

    @app.get("/api/queues")
    def get_queues(labeller: str | None = None) -> dict[str, Any]:
        return {"queues": session.queues(labeller), "labellers": session.labellers()}

    @app.get("/api/next")
    def get_next(labeller: str, queue: str | None = None) -> dict[str, Any]:
        try:
            labeller = clean_name(labeller)
        except LabelError as exc:
            raise fail(exc) from None
        if queue and queue not in dataset.queues:
            raise HTTPException(404, f"unknown queue '{queue}'")
        item = session.next_item(labeller, queue or None)
        if item is None:
            return {"done": True, "item": None}
        return {"done": False, "item": item_payload(session, item, labeller)}

    @app.get("/api/item/{item_id}")
    def get_item(item_id: str, labeller: str | None = None) -> dict[str, Any]:
        item = dataset.by_id.get(item_id)
        if item is None:
            raise HTTPException(404, f"unknown item '{item_id}'")
        if labeller:
            session.hold(labeller.strip(), item_id)
        return {"item": item_payload(session, item, labeller)}

    @app.post("/api/label")
    def post_label(body: LabelIn) -> dict[str, Any]:
        try:
            rec = session.label(
                body.item_id, body.labeller, body.decision, skip=body.skip, note=body.note
            )
        except LabelError as exc:
            raise fail(exc) from None
        return {"record": rec.model_dump(mode="json")}

    @app.post("/api/undo")
    def post_undo(body: UndoIn) -> dict[str, Any]:
        try:
            res = session.undo(body.labeller)
        except LabelError as exc:
            raise fail(exc) from None
        if res is None:
            return {"undone": None, "item": None}
        undo_rec, target = res
        item = dataset.by_id.get(target.item_id)
        return {
            "undone": target.model_dump(mode="json"),
            "record": undo_rec.model_dump(mode="json"),
            "item": item_payload(session, item, body.labeller) if item else None,
        }

    @app.get("/api/agreement")
    def get_agreement() -> dict[str, Any]:
        return agreement(session)

    @app.get("/api/image/{item_id}/{n}")
    def get_image(item_id: str, n: int) -> FileResponse:
        item = dataset.by_id.get(item_id)
        if item is None:
            raise HTTPException(404, "unknown item")
        path = resolve_image(item, n, config, root)
        return FileResponse(
            path,
            media_type=IMAGE_TYPES[path.suffix.lower()],
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
            },
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/{rest:path}")
    def api_404(rest: str) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
