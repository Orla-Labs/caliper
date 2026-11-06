"""Helpers for the project-mode API tests (dev 3): a fictional project in memory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from caliper_label.app import create_app
from caliper_label.context import MemoryLabels, ProjectContext, RowsItems
from caliper_label.project import ProjectConfig
from fastapi.testclient import TestClient

ROWS: list[dict[str, Any]] = [
    {
        "ticket_id": f"t{i}",
        "subject": f"Fictional ticket {i}",
        "body": f"Order {1000 + i} from Moonwick Ltd (fictional) did not arrive.",
        "priority": "high" if i % 3 == 0 else "low",
        "team": ["billing", "shipping"][i % 2],
        "known": None,
        "score": i / 10,
    }
    for i in range(12)
]

BASE: dict[str, Any] = {
    "name": "Ticket triage (fictional)",
    "source": {"kind": "file", "path": "data/tickets.csv", "id_column": "ticket_id"},
    "card": {
        "fields": [
            {"column": "subject", "type": "text"},
            {"column": "body", "type": "long_text"},
            {"column": "team", "type": "badge"},
            {"column": "score", "type": "number", "decimals": 1},
        ]
    },
    "labels": {
        "type": "single",
        "options": [
            {"value": "correct", "label": "Correct", "hotkey": "1", "approved": True},
            {"value": "wrong", "label": "Wrong", "hotkey": "2"},
            {"value": "unsure", "label": "Unsure", "hotkey": "3", "requires_note": True},
        ],
        "category_column": "team",
    },
    "workflow": {"queues": {"by": "priority"}, "stage": "triage"},
}


def merge(base: dict[str, Any], **over: Any) -> dict[str, Any]:
    out = {**base}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def make_ctx(
    tmp_path: Path, rows: list[dict[str, Any]] | None = None, **over: Any
) -> ProjectContext:
    cfg = ProjectConfig.model_validate(merge(BASE, **over))
    return ProjectContext(
        tmp_path,
        cfg,
        store=None,
        connector=None,
        version=3,
        items=RowsItems(rows if rows is not None else ROWS, cfg),
        labels=MemoryLabels(),
    )


def client_for(ctx: ProjectContext | None, **kw: Any) -> TestClient:
    return TestClient(create_app(ctx=ctx, **kw))


def post(c: TestClient, item: str, decision: Any, who: str = "ana", **kw: Any) -> Any:
    return c.post(
        "/api/labels", json={"item_id": item, "labeller": who, "decision": decision, **kw}
    )


def next_ids(c: TestClient, who: str = "ana", n: int = 1, queue: str | None = None) -> list[str]:
    q = f"&queue={queue}" if queue else ""
    body = c.get(f"/api/next?labeller={who}&n={n}{q}").json()
    return [it["id"] for it in body["items"]]
