"""Team views: progress dashboard, agreement, adjudication."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .agreement import agreement, dashboard, disagreements
from .errors import ApiError
from .routes_label import Decision, fail, get_session, item_payload, record_payload
from .state import LabelError, Session

router = APIRouter(tags=["team"])
S = Annotated[Session, Depends(get_session)]


class AdjudicateIn(BaseModel):
    item_id: str
    adjudicator: str = Field(description="who sets the final label")
    decision: Decision = Field(description="same encoding as POST /api/labels")
    note: str | None = None


@router.get(
    "/api/dashboard",
    summary="Progress per labeller and queue, throughput, kappa, golden accuracy",
)
def get_dashboard(session: S) -> dict[str, Any]:
    return dashboard(session)


@router.get(
    "/api/agreement", summary="Inter-annotator kappa (overall, by category) and disagreements"
)
def get_agreement(session: S) -> dict[str, Any]:
    return agreement(session)


@router.get(
    "/api/adjudications",
    summary="Items labellers disagree on, side by side",
    description="`status`: `open` (no final label yet, default), `done`, or `all`.",
)
def list_adjudications(
    session: S,
    status: str = "open",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    if status not in ("open", "done", "all"):
        raise ApiError(400, f"unknown status '{status}'", "use open, done or all")
    rows = disagreements(session)
    counts = {
        "open": sum(1 for d in rows if d["adjudication"] is None),
        "done": sum(1 for d in rows if d["adjudication"] is not None),
    }
    counts["all"] = len(rows)
    if status == "open":
        rows = [d for d in rows if d["adjudication"] is None]
    elif status == "done":
        rows = [d for d in rows if d["adjudication"] is not None]
    page = rows[offset : offset + limit]
    for d in page:
        item = session.items.get(d["item_id"])
        d["item"] = item_payload(session, item, None) if item else None
    return {"total": len(rows), "counts": counts, "offset": offset, "items": page}


@router.post(
    "/api/adjudications",
    summary="Set an item's final label (source: adjudicated)",
    description="A later adjudication of the same item replaces the earlier one.",
)
def post_adjudication(session: S, body: AdjudicateIn) -> dict[str, Any]:
    try:
        rec = session.adjudicate(body.item_id, body.adjudicator, body.decision, body.note)
    except LabelError as exc:
        raise fail(exc) from None
    return {"record": record_payload(session, rec)}
