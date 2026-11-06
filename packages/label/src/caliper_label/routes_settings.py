"""Project settings API (dev 2): `/api/settings`.

    GET  /api/settings                 config (full JSON), the YAML file, path, revision,
                                       label counts per option value, validation issues
    POST /api/settings/plan  {config}  dry run: option migrations, issues, the YAML it would write
    PUT  /api/settings       {config, revision, confirm}
                                       save: migrations applied, revision + 1, file edited in
                                       place (comments kept), then `ctx.reload()`

`revision` in the PUT body is the revision the edit started from; a different on-disk revision
(someone saved meanwhile, or the file was hand-edited and bumped) answers 409. Removing an
option that has labels needs `confirm: true`: it is kept with `retired: true` and its labels stay.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .project import CONFIG_FILENAME, ProjectConfig, load_project, render_project, save_project
from .project_setup import errors, plan_label_migration, validate_project
from .routes_setup import ErrorShapeRoute, SetupError, _parse_config

router = APIRouter(tags=["settings"], route_class=ErrorShapeRoute)


def _ctx(request: Request) -> Any:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise SetupError(409, "no project open", "open or create a project from the start screen")
    return ctx


def _decision_values(decision: str) -> list[str]:
    """multi-choice decisions are stored as a sorted JSON array string (dev 3's state.py)."""
    if decision.startswith("["):
        try:
            vals = json.loads(decision)
        except ValueError:
            return [decision]
        if isinstance(vals, list):
            return [str(v) for v in vals]
    return [decision]


def label_counts(store: Any) -> dict[str, int]:
    """Effective labels per decision value (undone records and skips left out)."""
    if store is None:
        return {}
    if hasattr(store, "iter_labels"):
        records = [r for r, _v in store.iter_labels()]
    elif hasattr(store, "load"):
        records = list(store.load())
    else:
        return {}
    undone = {r.undoes for r in records if getattr(r, "undoes", None)}
    latest: dict[tuple[str, str], Any] = {}
    for r in records:
        if r.undoes or r.record_id in undone:
            continue
        latest[(r.item_id, r.labeller)] = r
    counts: Counter[str] = Counter()
    for r in latest.values():
        if r.skipped or r.decision is None:
            continue
        for value in _decision_values(str(r.decision)):
            counts[value] += 1
    return dict(counts)


def _payload(ctx: Any) -> dict[str, Any]:
    project_dir = Path(ctx.project_dir)
    path = project_dir / CONFIG_FILENAME
    cfg: ProjectConfig = ctx.config
    return {
        "config": cfg.model_dump(mode="json"),
        "yaml": path.read_text(encoding="utf-8") if path.exists() else render_project(cfg),
        "path": str(path),
        "project_dir": str(project_dir),
        "revision": cfg.revision,
        "version": getattr(ctx, "version", cfg.revision),
        "label_counts": label_counts(getattr(ctx, "store", None)),
        "issues": [i.model_dump() for i in validate_project(cfg)],
    }


@router.get("/api/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _payload(_ctx(request))


class PlanIn(BaseModel):
    config: dict[str, Any]


def _plan(ctx: Any, data: dict[str, Any]) -> tuple[ProjectConfig, Any, list[Any]]:
    new, issues = _parse_config(data)
    if new is None:
        first = issues[0]
        raise SetupError(422, f"{first.path}: {first.message}", first.hint or "fix it and retry")
    on_disk = load_project(ctx.project_dir)
    plan = plan_label_migration(on_disk.labels, new.labels, label_counts(ctx.store))
    new = new.model_copy(update={"labels": plan.labels})
    return new, plan, validate_project(new)


@router.post("/api/settings/plan")
def post_plan(body: PlanIn, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    new, plan, issues = _plan(ctx, body.config)
    path = Path(ctx.project_dir) / CONFIG_FILENAME
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    preview = new.model_copy(update={"revision": new.revision + 1})
    return {
        "plan": plan.model_dump(mode="json", exclude={"labels"}),
        "issues": [i.model_dump() for i in issues],
        "ok": not errors(issues),
        "yaml": render_project(preview, existing),
    }


class SaveIn(BaseModel):
    config: dict[str, Any]
    revision: int | None = None  # the revision this edit started from
    confirm: bool = False


@router.put("/api/settings")
def put_settings(body: SaveIn, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    on_disk = load_project(ctx.project_dir)
    if body.revision is not None and body.revision != on_disk.revision:
        raise SetupError(
            409,
            f"the project changed since you opened settings (revision {body.revision} -> "
            f"{on_disk.revision})",
            "reload settings and apply your change again",
        )
    new, plan, issues = _plan(ctx, body.config)
    errs = errors(issues)
    if errs:
        e = errs[0]
        raise SetupError(422, f"{e.path}: {e.message}", e.hint or "fix it and save again")
    if plan.needs_confirmation and not body.confirm:
        gone = ", ".join(c.value for c in plan.changes if c.kind == "retired")
        raise SetupError(
            409,
            f"removing {gone} would affect existing labels",
            "confirm to keep those labels and flag the option as retired",
        )
    new = new.model_copy(update={"revision": on_disk.revision + 1})
    save_project(ctx.project_dir, new)
    reload = getattr(ctx, "reload", None)
    if callable(reload):
        reload()
    else:
        ctx.config = new
    ctx = _ctx(request)  # reload may replace the context object
    out = _payload(ctx)
    out["changes"] = [c.model_dump() for c in plan.changes]
    return out
