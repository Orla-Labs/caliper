"""The labelling loop and the public REST API (items, labels, next, media).

Works the same in legacy mode (``serve DATA --config CFG``) and on a project:
routes get the :class:`~caliper_label.state.Session` from :func:`require_session`.
Legacy endpoint names (``/api/label``, ``/api/item/{id}``, ``/api/config``,
``/api/image/{id}/{n}``) are kept as aliases and hidden from the OpenAPI page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import is_authed
from .errors import ApiError
from .project import LabelType
from .state import ItemView, LabelError, Session, clean_name

router = APIRouter()

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
}
IMAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=300",
}


def get_session(request: Request) -> Session:
    from .context import require_session

    return require_session(request)


S = Annotated[Session, Depends(get_session)]


def fail(exc: LabelError) -> ApiError:
    return ApiError(exc.status, str(exc), exc.hint)


# --------------------------------------------------------------------------- models


Decision = str | list[str] | float | int | None


class LabelIn(BaseModel):
    item_id: str = Field(description="the item's id")
    labeller: str = Field(description="who is labelling (free text, max 80 characters)")
    decision: Decision = Field(
        default=None,
        description=(
            "single / yes_no_unsure: an option value; multi: a list of option values; "
            "score: a number; free_text: the text. Leave out when skip is true."
        ),
    )
    skip: bool = Field(default=False, description="not now: the item comes back after the rest")
    note: str | None = Field(default=None, description="why (required by some options)")


class UndoIn(BaseModel):
    labeller: str


# --------------------------------------------------------------------------- payloads


def item_payload(session: Session, item: ItemView, labeller: str | None) -> dict[str, Any]:
    st = session.state(item.id)
    mine = st.labels.get(labeller or "")
    golden = session.is_golden(item.id)
    return {
        "id": item.id,
        "index": item.ordinal,
        "queue": item.queue,
        "category": item.category,
        "fields": session.items.card(item),
        # golden checks look like any other item: no counts that would give them away
        "labels_so_far": 0 if golden else len(st.labels),
        "labels_needed": session.needed,
        "skipped_before": bool(labeller and labeller in st.skips),
        "my_decision": mine.decision if mine else None,
    }


def record_payload(session: Session, rec: Any) -> dict[str, Any]:
    from .export import retired_option

    d = rec.model_dump(mode="json")
    d["project_version"] = session.versions.get(rec.record_id)
    # the option was retired in Settings after this label was made: kept, flagged (LPF-C-9)
    d["retired_option"] = retired_option(session.rules, rec.decision)
    return d


def label_config(session: Session, request: Request) -> dict[str, Any]:
    rules = session.rules
    lab = session.labels_cfg
    ctx = getattr(request.app.state, "ctx", None)
    legacy_cfg = session.config
    name = ctx.config.name if ctx else (legacy_cfg.title if legacy_cfg else "caliper label")
    score = None
    if rules.type is LabelType.SCORE:
        r = rules.score
        score = {"min": r.min, "max": r.max, "step": r.step}
    return {
        "name": name,
        "description": ctx.config.description if ctx else None,
        "instructions": session.workflow.instructions,
        "mode": "project" if ctx else "legacy",
        "version": session.project_version,
        "type": rules.type.value,
        "options": [
            {
                "value": o.value,
                "label": o.label or o.value,
                "hotkey": (o.hotkey or "").lower() or None,
                "color": o.color,
                "approved": o.approved,
                "requires_note": o.requires_note,
                "retired": o.retired,
            }
            for o in rules.options
        ],
        "score": score,
        "keys": lab.keys.model_dump(),
        "queues": session.items.queues(),
        "n_items": session.items.count(None),
        "double_label": session.needed,
        "assignment": session.workflow.assignment.value,
        "golden": bool(session.workflow.golden),
        "default_labeller": getattr(request.app.state, "default_labeller", None),
        "auth": "token" if getattr(request.app.state, "auth_token", None) else "none",
    }


# --------------------------------------------------------------------------- meta


@router.get("/api/status", tags=["meta"], summary="Is a project open? Is a token needed?")
def status(request: Request) -> dict[str, Any]:
    st = request.app.state
    auth = "token" if getattr(st, "auth_token", None) else "none"
    if not is_authed(request):
        return {"auth": auth, "authed": False, "mode": None, "project": None}
    session = getattr(st, "session", None)
    ctx = getattr(st, "ctx", None)
    project = None
    mode = "none"
    if session is not None:
        mode = "legacy"
        project = {"name": session.config.title, "version": None, "dir": None}
    elif ctx is not None:
        mode = "project"
        project = {"name": ctx.config.name, "version": ctx.version, "dir": str(ctx.project_dir)}
    return {"auth": auth, "authed": True, "mode": mode, "project": project}


@router.get(
    "/api/label-config",
    tags=["labelling"],
    summary="What the labelling screen needs: label type, options, keys, queues",
)
def get_label_config(session: S, request: Request) -> dict[str, Any]:
    return label_config(session, request)


@router.get("/api/queues", tags=["labelling"], summary="Progress per queue (and for one labeller)")
def get_queues(session: S, labeller: str | None = None) -> dict[str, Any]:
    return {"queues": session.queues(labeller), "labellers": session.labellers()}


# --------------------------------------------------------------------------- serving


def _done_reason(session: Session, labeller: str, queue: str | None) -> str:
    if session.items.count(None) == 0:
        return "empty"
    if queue:
        others = [q for q in session.queues(labeller) if q["name"] != queue and q["left"] > 0]
        if others:
            return "queue_done"
    return "all_done"


@router.get(
    "/api/next",
    tags=["labelling"],
    summary="The next items for a labeller (prefetch with n)",
    description=(
        "Returns up to `n` items this labeller may label, in serving order, and leases "
        "them so other labellers are not served the same items. `item` is the first one. "
        "When nothing is left, `done` is true and `reason` says why: `queue_done` (other "
        "queues still have work), `all_done`, or `empty` (the project has no items)."
    ),
)
def get_next(
    session: S,
    labeller: str,
    queue: str | None = None,
    n: Annotated[int, Query(ge=1, le=20)] = 1,
) -> dict[str, Any]:
    try:
        labeller = clean_name(labeller)
    except LabelError as exc:
        raise fail(exc) from None
    if queue and queue not in session.items.queues():
        raise ApiError(404, f"unknown queue '{queue}'", "pick a queue from /api/queues")
    items = session.next_items(labeller, queue or None, n)
    payloads = [item_payload(session, it, labeller) for it in items]
    if not payloads:
        return {
            "done": True,
            "reason": _done_reason(session, labeller, queue),
            "item": None,
            "items": [],
        }
    return {"done": False, "reason": None, "item": payloads[0], "items": payloads}


@router.get(
    "/api/items",
    tags=["items"],
    summary="List items (paginated)",
    description="Items in snapshot order with their card fields and active labels.",
)
def list_items(
    session: S,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    queue: str | None = None,
) -> dict[str, Any]:
    page = session.items.page(offset, limit, queue)
    out = []
    for it in page:
        p = item_payload(session, it, None)
        st = session.state(it.id)
        p["labels"] = [
            {"labeller": r.labeller, "decision": r.decision, "labelled_at": r.labelled_at}
            for r in st.labels.values()
        ]
        p["adjudicated"] = st.adjudication.decision if st.adjudication else None
        out.append(p)
    return {"total": session.items.count(queue), "offset": offset, "limit": limit, "items": out}


def _one_item(session: Session, item_id: str, labeller: str | None) -> dict[str, Any]:
    item = session.items.get(item_id)
    if item is None:
        raise ApiError(404, f"unknown item '{item_id}'")
    if labeller and labeller.strip():
        session.hold(labeller.strip(), item_id)
    return {"item": item_payload(session, item, labeller)}


@router.get("/api/items/{item_id}", tags=["items"], summary="One item")
def get_item(session: S, item_id: str, labeller: str | None = None) -> dict[str, Any]:
    return _one_item(session, item_id, labeller)


@router.get("/api/item/{item_id}", include_in_schema=False)
def get_item_legacy(session: S, item_id: str, labeller: str | None = None) -> dict[str, Any]:
    return _one_item(session, item_id, labeller)


# --------------------------------------------------------------------------- writing


def _post_label(session: Session, body: LabelIn) -> dict[str, Any]:
    try:
        rec = session.label(
            body.item_id, body.labeller, body.decision, skip=body.skip, note=body.note
        )
    except LabelError as exc:
        raise fail(exc) from None
    return {"record": record_payload(session, rec)}


@router.post(
    "/api/labels",
    tags=["labels"],
    summary="Record a label (or a skip)",
    description=(
        "Appends one label to the log. 409 if this labeller already labelled the item "
        "(undo first); 422 if the chosen option needs a note; 400 for an unknown or "
        "retired option."
    ),
)
def post_labels(session: S, body: LabelIn) -> dict[str, Any]:
    return _post_label(session, body)


@router.post("/api/label", include_in_schema=False)
def post_label_legacy(session: S, body: LabelIn) -> dict[str, Any]:
    return _post_label(session, body)


@router.get(
    "/api/labels",
    tags=["labels"],
    summary="Fetch labels (paginated)",
    description=(
        "Label records in log order. By default only active ones (not undone); "
        "`all=true` returns the raw log including skips, undos and adjudications."
    ),
)
def get_labels(
    session: S,
    item_id: str | None = None,
    labeller: str | None = None,
    all: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    recs = session.records if all else session.active
    rows = [
        r
        for r in recs
        if (item_id is None or r.item_id == item_id)
        and (labeller is None or r.labeller == labeller)
    ]
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "labels": [record_payload(session, r) for r in rows[offset : offset + limit]],
    }


@router.post("/api/undo", tags=["labels"], summary="Undo this labeller's last label or skip")
def post_undo(session: S, body: UndoIn) -> dict[str, Any]:
    try:
        res = session.undo(body.labeller)
    except LabelError as exc:
        raise fail(exc) from None
    if res is None:
        return {"undone": None, "item": None}
    undo_rec, target = res
    item = session.items.get(target.item_id)
    return {
        "undone": target.model_dump(mode="json"),
        "record": undo_rec.model_dump(mode="json"),
        "item": item_payload(session, item, body.labeller) if item else None,
    }


# --------------------------------------------------------------------------- legacy config


@router.get("/api/config", include_in_schema=False)
def get_config_legacy(session: S, request: Request) -> dict[str, Any]:
    config = session.config
    if config is None:
        return label_config(session, request)
    dataset = session.dataset
    return {
        "title": config.title,
        "instructions": config.instructions,
        "data_file": dataset.path.name,
        "data_kind": dataset.kind.value,
        "n_items": len(dataset.items),
        "decisions": [
            {"key": d.key, "value": d.value, "label": d.label or d.value, "approved": d.approved}
            for d in config.decisions
        ],
        "keys": config.keys.model_dump(),
        "queues": dataset.queues,
        "double_label": config.double_label,
        "default_labeller": getattr(request.app.state, "default_labeller", None),
        "store": session.store.path.name,
    }


# --------------------------------------------------------------------------- media


def resolve_image(item: Any, n: int, config: Any, root: Path) -> Path:
    """Local image path for field ``n`` of a legacy item. Never leaves ``root``."""
    from .config import FieldType
    from .legacy_session import _is_url, field_raw

    if n < 0 or n >= len(config.fields) or config.fields[n].type is not FieldType.IMAGE:
        raise ApiError(404, "no such image field")
    raw = field_raw(config.fields[n], item.raw)
    if not raw or not isinstance(raw, str) or _is_url(raw):
        raise ApiError(404, "no local image")
    return safe_local_image(raw, root)


def safe_local_image(raw: str, root: Path) -> Path:
    if "\x00" in raw:
        raise ApiError(400, "bad path")
    root = root.resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root):
        raise ApiError(403, "image path outside the image root")
    if target.suffix.lower() not in IMAGE_TYPES:
        raise ApiError(403, "not an image file type")
    if not target.is_file():
        raise ApiError(404, "image not found")
    return target


def _image_file(path: Path) -> FileResponse:
    return FileResponse(path, media_type=IMAGE_TYPES[path.suffix.lower()], headers=IMAGE_HEADERS)


@router.get("/api/image/{item_id}/{n}", include_in_schema=False)
def get_image_legacy(session: S, request: Request, item_id: str, n: int) -> FileResponse:
    if session.dataset is None:  # projects: /api/media/{item_id}/{field} (routes_data, dev 1)
        raise ApiError(404, "no such image", "projects serve images from /api/media/{item}/{field}")
    item = session.dataset.by_id.get(item_id)
    if item is None:
        raise ApiError(404, "unknown item")
    return _image_file(resolve_image(item, n, session.config, request.app.state.image_root))
