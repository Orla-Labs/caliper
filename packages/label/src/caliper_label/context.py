"""ProjectContext: the open project behind `app.state.ctx` (CONTRACT).

``app.state.ctx`` is ``None`` while the start screen is showing. Routes that need
a project use :func:`require_ctx` / :func:`require_session` and answer 409
``{"detail": "no project open", "hint": ...}`` otherwise. The start screen (dev 2)
opens a project at runtime with :func:`set_ctx`.

The labelling loop reads items through :class:`StoreItems` (paginated reads of
dev 1's ``ProjectStore`` snapshot; never everything at once) and writes labels
through :class:`StoreLabels`. :class:`RowsItems` / :class:`MemoryLabels` are the
same interfaces over plain Python rows, for tests and scripts.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from caliper_eval.types import LabelRecord
from fastapi import FastAPI, Request

from .config import get_path, render_template
from .errors import NO_PROJECT_HINT, ApiError
from .project import CardConfig, CardField, DisplayType, ProjectConfig
from .state import ItemView, Session

# --------------------------------------------------------------------------- card


def _is_url(v: str) -> bool:
    return v.startswith(("http://", "https://"))


def _lookup(display: dict[str, Any], f: CardField) -> Any:
    if f.template is not None:
        return render_template(f.template, display)
    if f.column is None:
        return None
    if f.column in display:
        return display[f.column]
    return get_path(display, f.column)


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def render_fields(
    card: CardConfig, display: dict[str, Any], item_id: str, hidden: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Card fields as JSON-safe display values. ``hidden`` columns are never sent."""
    secret = set(hidden)
    out: list[dict[str, Any]] = []
    for n, f in enumerate(card.fields):
        if f.hidden or (f.column and f.column in secret):
            continue
        raw = _lookup(display, f)
        label = f.label or f.column or f.template or f.type.value
        d: dict[str, Any] = {"type": f.type.value, "label": label, "n": n}
        if raw is None or raw == "":
            d["value"] = None
        elif f.type is DisplayType.NUMBER:
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                d["value"] = f"{raw:.{f.decimals}f}" if f.decimals is not None else str(raw)
            else:
                d["value"] = _text(raw)
        elif f.type is DisplayType.LINK:
            s = str(raw)
            d.update(value=s, href=s if _is_url(s) else None)
        elif f.type is DisplayType.IMAGE:
            s = str(raw)
            d.update(value=s, src=s if _is_url(s) else f"/api/media/{quote(item_id, safe='')}/{n}")
        elif f.type is DisplayType.JSON:
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    pass
            d.update(value=_text(raw), mono=True)
        else:
            d["value"] = _text(raw)
        out.append(d)
    if not card.fields:  # no card designed yet: show every displayed column as text
        for k, v in display.items():
            if k in secret:
                continue
            out.append({"type": "text", "label": k, "value": None if v in (None, "") else _text(v)})
    return out


def card_value(card: CardConfig, display: dict[str, Any], n: int) -> Any:
    if n < 0 or n >= len(card.fields):
        return None
    return _lookup(display, card.fields[n])


# --------------------------------------------------------------------------- item sources


def _scalar(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


class RowsItems:
    """An ItemSource over in-memory rows (tests, scripts). Columns are row keys."""

    def __init__(self, rows: list[dict[str, Any]], config: ProjectConfig) -> None:
        self.config = config
        wf = config.workflow
        idc = config.source.id_column
        self._items: list[ItemView] = []
        for i, row in enumerate(rows):
            iid = _scalar(row.get(idc)) if idc else None
            queue = _scalar(row.get(wf.queues.by)) if wf.queues.by else "all"
            cat = config.labels.category_column
            gold = _scalar(row.get(wf.golden.answer_column)) if wf.golden else None
            self._items.append(
                ItemView(
                    id=iid or f"row-{i:04d}",
                    ordinal=i,
                    queue=queue or "(none)",
                    category=_scalar(row.get(cat)) if cat else None,
                    golden_answer=gold,
                    data=dict(row),
                )
            )
        self._by_id = {it.id: it for it in self._items}
        self._queues = list(dict.fromkeys(it.queue for it in self._items))

    def queues(self) -> list[str]:
        return list(self._queues)

    def _list(self, queue: str | None) -> list[ItemView]:
        return self._items if queue is None else [it for it in self._items if it.queue == queue]

    def count(self, queue: str | None = None) -> int:
        return len(self._list(queue))

    def page(self, offset: int, limit: int, queue: str | None = None) -> list[ItemView]:
        return self._list(queue)[offset : offset + limit]

    def get(self, item_id: str) -> ItemView | None:
        return self._by_id.get(item_id)

    def golden_ids(self) -> list[str]:
        return [it.id for it in self._items if it.golden_answer]

    def display(self, item: ItemView) -> dict[str, Any]:
        return item.data

    def card(self, item: ItemView) -> list[dict[str, Any]]:
        return render_fields(self.config.card, item.data, item.id, _secret_columns(self.config))


def _secret_columns(config: ProjectConfig) -> list[str]:
    return [config.workflow.golden.answer_column] if config.workflow.golden else []


def _get(row: Any, *names: str) -> Any:
    for n in names:
        if isinstance(row, dict):
            if n in row:
                return row[n]
        elif hasattr(row, n):
            return getattr(row, n)
    return None


class StoreItems:
    """ItemSource over dev 1's ``ProjectStore`` (paginated SQLite reads of the snapshot)."""

    def __init__(self, store: Any, config: ProjectConfig) -> None:
        self.store = store
        self.config = config
        self._golden: list[str] | None = None
        self._counts: dict[str | None, tuple[int, float]] = {}
        self._queues: tuple[list[str], float] | None = None

    def _view(self, row: Any) -> ItemView:
        disp = _get(row, "fields", "display", "display_json")
        if isinstance(disp, str):
            disp = json.loads(disp)
        return ItemView(
            id=str(_get(row, "item_id", "id")),
            ordinal=int(_get(row, "ordinal") or 0),
            queue=_get(row, "queue") or "all",
            category=_get(row, "category"),
            golden_answer=_scalar(_get(row, "golden_answer")),
            data=disp or {},
        )

    def queues(self) -> list[str]:
        """Queue names in snapshot order. ``ProjectStore.queues()`` gives (name, n_active)."""
        now = time.monotonic()
        if self._queues and now - self._queues[1] < self.COUNT_TTL:
            return list(self._queues[0])
        if not hasattr(self.store, "queues"):
            return ["all"]
        names: list[str] = []
        for q in self.store.queues():
            if isinstance(q, tuple):
                names.append(str(q[0]))
                self._counts[str(q[0])] = (int(q[1]), now)  # the counts come for free
            else:
                names.append(str(q))
        self._queues = (names, now)
        return list(names)

    COUNT_TTL = 5.0  # seconds; counts only change on "refresh from source"

    def count(self, queue: str | None = None) -> int:
        now = time.monotonic()
        hit = self._counts.get(queue)
        if hit and now - hit[1] < self.COUNT_TTL:
            return hit[0]
        n = int(self.store.count(queue))
        self._counts[queue] = (n, now)
        return n

    def page(self, offset: int, limit: int, queue: str | None = None) -> list[ItemView]:
        return [self._view(r) for r in self.store.items(offset, limit, queue)]

    def get(self, item_id: str) -> ItemView | None:
        row = self.store.item(item_id)
        return self._view(row) if row is not None else None

    def golden_ids(self) -> list[str]:
        if self._golden is None:
            if hasattr(self.store, "execute"):  # dev 1's ProjectStore: one indexed query
                rows = self.store.execute(
                    "SELECT item_id FROM items WHERE golden_answer IS NOT NULL "
                    "AND golden_answer <> '' AND removed = 0 ORDER BY ordinal"
                )
                self._golden = [r[0] for r in rows]
            else:  # fall back to one paged pass, keeping ids only
                ids: list[str] = []
                off, total = 0, self.count()
                while off < total:
                    page = self.page(off, 1000)
                    ids += [it.id for it in page if it.golden_answer]
                    off += len(page) or total
                self._golden = ids
        return list(self._golden)

    def display(self, item: ItemView) -> dict[str, Any]:
        return item.data

    def card(self, item: ItemView) -> list[dict[str, Any]]:
        return render_fields(self.config.card, item.data, item.id, _secret_columns(self.config))


# --------------------------------------------------------------------------- label logs


class MemoryLabels:
    """A LabelLog in memory (tests, scripts)."""

    def __init__(self) -> None:
        self.rows: list[tuple[LabelRecord, int | None]] = []

    def load(self) -> list[tuple[LabelRecord, int | None]]:
        return list(self.rows)

    def append(self, record: LabelRecord, project_version: int | None) -> None:
        self.rows.append((record, project_version))


class StoreLabels:
    """LabelLog over dev 1's ``ProjectStore`` (``labels()`` / ``append_label()``)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def load(self) -> list[tuple[LabelRecord, int | None]]:
        if hasattr(self.store, "iter_labels"):
            return list(self.store.iter_labels())
        out: list[tuple[LabelRecord, int | None]] = []
        for row in self.store.labels():
            if isinstance(row, tuple):
                rec, ver = row
            elif isinstance(row, LabelRecord):
                rec, ver = row, None
            else:
                rec = _get(row, "record")
                ver = _get(row, "project_version")
                if rec is None:
                    d = dict(row)
                    ver = d.pop("project_version", None)
                    rec = LabelRecord.model_validate(d)
            out.append((rec, ver))
        return out

    def append(self, record: LabelRecord, project_version: int | None) -> None:
        self.store.append_label(record, project_version)


# --------------------------------------------------------------------------- context


@dataclass
class ProjectContext:
    project_dir: Path
    config: ProjectConfig
    store: Any  # ProjectStore (dev 1)
    connector: Any  # Connector (dev 1); may be None for an offline snapshot
    version: int = 1
    items: Any = None  # ItemSource; default StoreItems(store, config)
    labels: Any = None  # LabelLog; default StoreLabels(store)
    loader: Callable[[Path], ProjectConfig] | None = None  # dev 2's load_project
    session: Session = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = StoreItems(self.store, self.config)
        if self.labels is None:
            self.labels = StoreLabels(self.store)
        self.session = Session.over(
            self.items,
            self.labels,
            self.config.labels,
            self.config.workflow,
            project_version=self.version,
        )

    def reload(self, config: ProjectConfig | None = None) -> None:
        """Settings were saved: re-read the config, take its revision, keep the labels."""
        with self._lock:
            if config is None and self.loader is not None:
                config = self.loader(self.project_dir)
            if config is not None:
                self.config = config
                if hasattr(self.items, "config"):
                    self.items.config = config
                if hasattr(self.items, "_golden"):
                    self.items._golden = None
                if hasattr(self.items, "_counts"):
                    self.items._counts.clear()
                    self.items._queues = None
            rev = getattr(self.config, "revision", None)
            self.version = int(rev) if rev and rev > self.version else self.version + 1
            if hasattr(self.store, "set_project_version"):
                self.store.set_project_version(self.version)
            self.session.reconfigure(self.config.labels, self.config.workflow, self.version)

    @classmethod
    def open(cls, project_dir: str | Path) -> ProjectContext:
        """Open a project folder: config (dev 2), store + connector + snapshot (dev 1).

        The first open takes the snapshot. Later opens label from the snapshot even
        when the source cannot be reached right now (the connector is then None and
        "Refresh from source" reports the connection error).
        """
        from . import project as project_mod
        from . import snapshot
        from .connectors import ConnectorError, open_connector
        from .project_store import ProjectStore

        d = Path(project_dir).resolve()
        load = getattr(project_mod, "load_project", None)
        if load is None:  # pragma: no cover - dev 2's loader not merged
            raise ApiError(501, "opening a project folder needs caliper_label.project.load_project")
        config = load(d)
        store = ProjectStore.open(d)
        try:
            connector = open_connector(config.source, d)
        except ConnectorError:
            if not store.has_snapshot():
                raise
            connector = None
        if not store.has_snapshot():
            snapshot.take(_SnapshotArgs(d, config, store, connector))
        version = int(getattr(config, "revision", 1) or 1)
        store.set_project_version(version)
        return cls(d, config, store, connector, version=version, loader=load)


@dataclass
class _SnapshotArgs:
    project_dir: Path
    config: ProjectConfig
    store: Any
    connector: Any


# --------------------------------------------------------------------------- app helpers


def set_ctx(app: FastAPI, ctx: ProjectContext | None) -> None:
    """Open (or close) the project the server works on. The start screen calls this."""
    app.state.ctx = ctx


def require_ctx(request: Request) -> ProjectContext:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise ApiError(409, "no project open", NO_PROJECT_HINT)
    return ctx


def require_session(request: Request) -> Session:
    """The labelling session: legacy ``serve`` or the open project's."""
    s = getattr(request.app.state, "session", None)
    if s is not None:
        return s
    return require_ctx(request).session
