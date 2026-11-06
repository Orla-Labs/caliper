"""Snapshot the source into the ProjectStore, and refresh it with a reviewable diff (CONTRACT).

What a snapshot keeps per item: the id, its queue / category / golden answer (from the columns
named in the config), and ``display_json``: the top-level columns the card shows (dotted card
paths keep their top-level column; templates keep the columns they reference). With no card
fields yet, every column is kept. ``row_hash`` is a sha256 over exactly that, so a refresh only
reports changes the labeller would see.

* ``take(ctx)``: first snapshot (streams in batches; a no-op when one exists).
* ``diff(ctx)``: stream the source into a staging table, compare by id and row hash ->
  ``SnapshotDiff`` (counts + first ids of each kind; ``diff_ids`` pages through the rest).
* ``apply(ctx, diff)``: new items appended, changed items updated, removed items flagged
  ``removed`` (their labels are kept), history recorded.

Without ``source.id_column`` (and no connector default such as ``image`` for folders) ids are
row numbers, so a refresh can only line rows up by position; the diff carries a warning.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from .config import get_path
from .connectors.base import Connector, ConnectorError, jsonable
from .project import ProjectConfig
from .project_store import DEFAULT_QUEUE, ProjectStore, SnapshotInfo

BATCH = 5000
SAMPLE = 100
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class SnapshotContext(Protocol):
    """What snapshot needs from dev 3's ``ProjectContext`` (any object with these works)."""

    project_dir: Path
    config: ProjectConfig
    store: ProjectStore
    connector: Connector


class SnapshotDiff(BaseModel):
    diff_id: str
    base_version: int  # store items_version the diff was computed against
    n_source: int
    n_new: int
    n_removed: int
    n_changed: int
    n_unchanged: int
    new: list[str]  # first SAMPLE ids of each kind; page with diff_ids()
    removed: list[str]
    changed: list[str]
    removed_with_labels: int = 0
    warning: str | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scalar(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, list):
        return ",".join(str(x) for x in v) if v else None
    if isinstance(v, dict):
        return _canonical(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _root(path: str, keys: Iterable[str]) -> str:
    return path if path in keys else path.split(".", 1)[0]


def _lookup(row: dict[str, Any], path: str | None) -> Any:
    if not path:
        return None
    if path in row:
        return row[path]
    return get_path(row, path)


class _RowMapper:
    """Turns source rows into item tuples according to the project config."""

    def __init__(self, config: ProjectConfig, connector: Connector, n_total: int | None):
        self.cfg = config
        self.id_col = config.source.id_column or getattr(connector, "default_id_column", None)
        self.queue_col = config.workflow.queues.by
        self.cat_col = config.labels.category_column
        self.gold_col = config.workflow.golden.answer_column if config.workflow.golden else None
        self.width = max(4, len(str(n_total))) if n_total is not None else 4
        self.columns: list[str] | None = None  # resolved on the first row

    def _resolve_columns(self, row: dict[str, Any]) -> list[str] | None:
        fields = self.cfg.card.fields
        if not fields:
            return None  # keep everything until a card is designed
        keys = list(row)
        cols: dict[str, None] = {}
        for f in fields:
            if f.column:
                cols[_root(f.column, keys)] = None
            if f.template:
                for m in _PLACEHOLDER.finditer(f.template):
                    cols[_root(m.group(1), keys)] = None
        return list(cols)

    def __call__(self, ordinal: int, row: dict[str, Any]) -> tuple:
        if self.columns is None and self.cfg.card.fields:
            self.columns = self._resolve_columns(row)
        if self.id_col:
            item_id = _scalar(_lookup(row, self.id_col))
            if item_id is None:
                raise ConnectorError(
                    f"row {ordinal + 1} has no value in the id column '{self.id_col}'",
                    "pick an id column with a value on every row, or clear source.id_column to "
                    "use row numbers",
                )
        else:
            item_id = f"row-{ordinal:0{self.width}d}"
        queue = DEFAULT_QUEUE
        if self.queue_col:
            queue = _scalar(_lookup(row, self.queue_col)) or "(none)"
        category = _scalar(_lookup(row, self.cat_col)) if self.cat_col else None
        golden = _scalar(_lookup(row, self.gold_col)) if self.gold_col else None
        shown = row if self.columns is None else {c: row.get(c) for c in self.columns}
        display = _canonical(jsonable(shown))
        h = hashlib.sha256(
            _canonical([display, queue, category, golden]).encode("utf-8")
        ).hexdigest()[:32]
        return (item_id, ordinal, queue, category, golden, display, h)


def _dup_error(store: ProjectStore, table: str, batch: list[tuple], id_col: str | None, extra=()):
    """Find the offending id after a UNIQUE failure: the first row whose id is already stored
    under another ordinal (rows before it in the batch were inserted with their own ordinal)."""
    q = f"SELECT ordinal FROM {table} WHERE item_id = ?" + (" AND diff_id = ?" if extra else "")
    dup = batch[0][0]
    for t in batch:
        row = store._db.execute(q, (t[0], *extra)).fetchone()
        if row is not None and row[0] != t[1]:
            dup = t[0]
            break
    return ConnectorError(
        f"duplicate id '{dup}' in column '{id_col or 'row number'}'",
        "pick a column whose values are unique per row as the id column",
    )


def _count_or_none(connector: Connector) -> int | None:
    try:
        return int(connector.count())
    except ConnectorError:
        raise
    except Exception:
        return None


def _source_json(config: ProjectConfig) -> str:
    return _canonical(config.source.model_dump(mode="json", exclude_defaults=True))


def _bump(db) -> int:
    row = db.execute("SELECT value FROM meta WHERE key = 'items_version'").fetchone()
    v = int(row[0]) + 1 if row else 1
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('items_version', ?)", (str(v),))
    return v


def take(ctx: SnapshotContext, *, batch_size: int = BATCH) -> SnapshotInfo:
    """First snapshot of the source. Returns the latest snapshot unchanged if one exists."""
    store, config, conn = ctx.store, ctx.config, ctx.connector
    existing = store.latest_snapshot()
    if existing is not None:
        return existing
    mapper = _RowMapper(config, conn, None if config.source.id_column else _count_or_none(conn))
    n = 0
    with store.transaction() as db:
        for batch in conn.iter_rows(batch_size=batch_size):
            tuples = [mapper(n + i, r) for i, r in enumerate(batch)]
            try:
                db.executemany(
                    "INSERT INTO items (item_id, ordinal, queue, category, golden_answer, "
                    "display_json, row_hash, added_snapshot) VALUES (?,?,?,?,?,?,?,NULL)",
                    tuples,
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise _dup_error(store, "items", tuples, mapper.id_col) from None
                raise
            n += len(tuples)
        if n == 0:
            raise ConnectorError("the source has no rows", "check the file, table or query")
        cur = db.execute(
            "INSERT INTO snapshots (taken_at, kind, n_items, n_new, source_json) "
            "VALUES (?, 'take', ?, ?, ?)",
            (_now(), n, n, _source_json(config)),
        )
        sid = cur.lastrowid
        db.execute("UPDATE items SET added_snapshot = ?", (sid,))
        _bump(db)
    info = store.latest_snapshot()
    assert info is not None
    return info


def diff(ctx: SnapshotContext, *, batch_size: int = BATCH, sample: int = SAMPLE) -> SnapshotDiff:
    """Read the source again and compare with the snapshot (nothing changes until ``apply``)."""
    store, config, conn = ctx.store, ctx.config, ctx.connector
    if not store.has_snapshot():
        raise ConnectorError("no snapshot yet", "start the project first (snapshot.take)")
    mapper = _RowMapper(config, conn, None if config.source.id_column else _count_or_none(conn))
    diff_id = uuid.uuid4().hex
    base = int(store.meta("items_version", "0") or 0)
    with store.transaction() as db:
        db.execute("DELETE FROM staged")  # one pending diff at a time
    n = 0
    for batch in conn.iter_rows(batch_size=batch_size):
        tuples = [(diff_id, *mapper(n + i, r)) for i, r in enumerate(batch)]
        with store.transaction() as db:
            try:
                db.executemany(
                    "INSERT INTO staged (diff_id, item_id, ordinal, queue, category, "
                    "golden_answer, display_json, row_hash) VALUES (?,?,?,?,?,?,?,?)",
                    tuples,
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise _dup_error(
                        store, "staged", [t[1:] for t in tuples], mapper.id_col, (diff_id,)
                    ) from None
                raise
        n += len(tuples)

    def ids(which: str) -> tuple[int, list[str]]:
        total = int(store.execute(_DIFF_COUNT[which], (diff_id,))[0][0])
        page = diff_ids(store, diff_id, which, 0, sample)  # type: ignore[arg-type]
        return total, page

    n_new, new = ids("new")
    n_removed, removed = ids("removed")
    n_changed, changed = ids("changed")
    removed_with_labels = int(
        store.execute(
            "SELECT count(DISTINCT l.item_id) FROM labels l JOIN items i ON i.item_id = l.item_id "
            "WHERE i.removed = 0 AND NOT EXISTS "
            "(SELECT 1 FROM staged s WHERE s.diff_id = ? AND s.item_id = i.item_id)",
            (diff_id,),
        )[0][0]
    )
    warning = None
    if not mapper.id_col:
        warning = (
            "no id column is set, so items are matched by row number: inserted or deleted "
            "rows in the source shift every later item. Set source.id_column for stable refreshes."
        )
    return SnapshotDiff(
        diff_id=diff_id,
        base_version=base,
        n_source=n,
        n_new=n_new,
        n_removed=n_removed,
        n_changed=n_changed,
        n_unchanged=n - n_new - n_changed,
        new=new,
        removed=removed,
        changed=changed,
        removed_with_labels=removed_with_labels,
        warning=warning,
    )


_DIFF_SQL = {
    "new": (
        "SELECT s.item_id FROM staged s LEFT JOIN items i ON i.item_id = s.item_id "
        "WHERE s.diff_id = ? AND (i.item_id IS NULL OR i.removed = 1) ORDER BY s.ordinal"
    ),
    "removed": (
        "SELECT i.item_id FROM items i WHERE i.removed = 0 AND NOT EXISTS "
        "(SELECT 1 FROM staged s WHERE s.diff_id = ? AND s.item_id = i.item_id) ORDER BY i.ordinal"
    ),
    "changed": (
        "SELECT s.item_id FROM staged s JOIN items i ON i.item_id = s.item_id "
        "WHERE s.diff_id = ? AND i.removed = 0 AND i.row_hash <> s.row_hash ORDER BY i.ordinal"
    ),
}
_DIFF_COUNT = {
    k: "SELECT count(*) FROM (" + v.rsplit(" ORDER BY", 1)[0] + ")" for k, v in _DIFF_SQL.items()
}


def diff_ids(
    store: ProjectStore,
    diff_id: str,
    which: Literal["new", "removed", "changed"],
    offset: int = 0,
    limit: int = 100,
) -> list[str]:
    """Page through the ids of one kind in a pending diff."""
    if which not in _DIFF_SQL:
        raise ConnectorError(f"unknown diff kind '{which}'", "use new, removed or changed")
    sql = _DIFF_SQL[which] + " LIMIT ? OFFSET ?"
    return [r[0] for r in store.execute(sql, (diff_id, int(limit), int(offset)))]


def apply(ctx: SnapshotContext, d: SnapshotDiff | str, *, note: str | None = None) -> SnapshotInfo:
    """Apply a pending diff. Stale diffs (snapshot changed since) are refused."""
    store, config = ctx.store, ctx.config
    diff_id = d if isinstance(d, str) else d.diff_id
    with store.transaction() as db:
        if not db.execute("SELECT 1 FROM staged WHERE diff_id = ? LIMIT 1", (diff_id,)).fetchone():
            raise ConnectorError(
                "this refresh is no longer pending", "run Refresh from source again"
            )
        if not isinstance(d, str):
            row = db.execute("SELECT value FROM meta WHERE key = 'items_version'").fetchone()
            if int(row[0] if row else 0) != d.base_version:
                raise ConnectorError(
                    "the snapshot changed since this diff was made", "run Refresh again"
                )
        counts = {k: int(db.execute(_DIFF_COUNT[k], (diff_id,)).fetchone()[0]) for k in _DIFF_SQL}
        cur = db.execute(
            "INSERT INTO snapshots (taken_at, kind, n_items, n_new, n_removed, n_changed, "
            "source_json, note) VALUES (?, 'refresh', 0, ?, ?, ?, ?, ?)",
            (
                _now(),
                counts["new"],
                counts["removed"],
                counts["changed"],
                _source_json(config),
                note,
            ),
        )
        sid = cur.lastrowid
        # removed: flag (labels stay)
        db.execute(
            "UPDATE items SET removed = 1, changed_snapshot = ? WHERE removed = 0 AND NOT EXISTS "
            "(SELECT 1 FROM staged s WHERE s.diff_id = ? AND s.item_id = items.item_id)",
            (sid, diff_id),
        )
        # changed or restored: update in place (ordinal kept)
        db.execute(
            "UPDATE items SET queue = s.queue, category = s.category, "
            "golden_answer = s.golden_answer, display_json = s.display_json, "
            "row_hash = s.row_hash, removed = 0, changed_snapshot = ? "
            "FROM staged s WHERE s.diff_id = ? AND s.item_id = items.item_id "
            "AND (items.row_hash <> s.row_hash OR items.removed = 1)",
            (sid, diff_id),
        )
        # new: appended after the current last ordinal, in source order
        base = db.execute("SELECT coalesce(max(ordinal), -1) + 1 FROM items").fetchone()[0]
        db.execute(
            "INSERT INTO items (item_id, ordinal, queue, category, golden_answer, display_json, "
            "row_hash, added_snapshot) "
            "SELECT s.item_id, ? + s.ordinal, s.queue, s.category, s.golden_answer, "
            "s.display_json, s.row_hash, ? FROM staged s "
            "WHERE s.diff_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM items i WHERE i.item_id = s.item_id)",
            (base, sid, diff_id),
        )
        n_items = db.execute("SELECT count(*) FROM items WHERE removed = 0").fetchone()[0]
        db.execute("UPDATE snapshots SET n_items = ? WHERE id = ?", (n_items, sid))
        db.execute("DELETE FROM staged")
        _bump(db)
    info = store.latest_snapshot()
    assert info is not None
    return info


def refresh(ctx: SnapshotContext, *, note: str | None = None) -> tuple[SnapshotDiff, SnapshotInfo]:
    """diff + apply in one go (CLI/scripts)."""
    d = diff(ctx)
    return d, apply(ctx, d, note=note)
