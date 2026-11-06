"""ProjectStore: the project's SQLite state at ``<project>/.caliper-label/project.db`` (CONTRACT).

Tables:

* ``items``: the snapshot. One row per item: ``item_id, ordinal, queue, category,
  golden_answer, display_json, row_hash`` plus ``removed`` (gone from the source on a refresh;
  its labels are kept) and the snapshot ids that added/last changed it.
* ``labels``: append-only ``LabelRecord`` bodies plus ``project_version`` (the config revision
  the label was made under). Never rewritten or deleted.
* ``snapshots``: history of takes and applied refreshes.
* ``staged``: rows of a pending refresh diff (see ``snapshot.py``).

Reads are paginated (``items(offset, limit)``; ``after=`` gives keyset paging for deep pages),
so nothing loads the whole snapshot. ``ProjectStore`` also satisfies the legacy ``LabelStore``
protocol (``path``, ``load``, ``append``, ``close``) so ``state.Session`` can run on it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from caliper_eval.types import LabelRecord
from pydantic import BaseModel

from .project import STATE_DIRNAME

DB_FILENAME = "project.db"
SCHEMA_VERSION = 1
DEFAULT_QUEUE = "all"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL,
  queue TEXT NOT NULL DEFAULT 'all',
  category TEXT,
  golden_answer TEXT,
  display_json TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  removed INTEGER NOT NULL DEFAULT 0,
  added_snapshot INTEGER,
  changed_snapshot INTEGER
);
CREATE INDEX IF NOT EXISTS items_by_ordinal ON items (removed, ordinal);
CREATE INDEX IF NOT EXISTS items_by_queue ON items (queue, removed, ordinal);
CREATE TABLE IF NOT EXISTS labels (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id TEXT UNIQUE NOT NULL,
  item_id TEXT NOT NULL,
  labeller TEXT NOT NULL,
  project_version INTEGER,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS labels_by_item ON labels (item_id);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  taken_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  n_items INTEGER NOT NULL,
  n_new INTEGER NOT NULL DEFAULT 0,
  n_removed INTEGER NOT NULL DEFAULT 0,
  n_changed INTEGER NOT NULL DEFAULT 0,
  source_json TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS staged (
  diff_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  queue TEXT NOT NULL,
  category TEXT,
  golden_answer TEXT,
  display_json TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  PRIMARY KEY (diff_id, item_id)
);
"""


class StoredItem(BaseModel):
    item_id: str
    ordinal: int
    queue: str
    category: str | None = None
    golden_answer: str | None = None
    fields: dict[str, Any]  # the snapshotted columns (display_json)
    row_hash: str
    removed: bool = False


class SnapshotInfo(BaseModel):
    id: int
    taken_at: str
    kind: str  # "take" | "refresh"
    n_items: int
    n_new: int = 0
    n_removed: int = 0
    n_changed: int = 0
    note: str | None = None


_ITEM_COLS = "item_id, ordinal, queue, category, golden_answer, display_json, row_hash, removed"


def _item(row: tuple) -> StoredItem:
    return StoredItem(
        item_id=row[0],
        ordinal=row[1],
        queue=row[2],
        category=row[3],
        golden_answer=row[4],
        fields=json.loads(row[5]),
        row_hash=row[6],
        removed=bool(row[7]),
    )


def state_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / STATE_DIRNAME


def ensure_state_dir(project_dir: str | Path) -> Path:
    """Create ``.caliper-label/`` with a ``.gitignore`` of ``*`` (CONTRACT)."""
    d = state_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return d


class ProjectStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @classmethod
    def open(cls, project_dir: str | Path) -> ProjectStore:
        return cls(ensure_state_dir(project_dir) / DB_FILENAME)

    # ------------------------------------------------------------------ low level

    def execute(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``with store.transaction() as db:`` -> one atomic write (BEGIN IMMEDIATE)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def meta(self, key: str, default: str | None = None) -> str | None:
        rows = self.execute("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------ items

    def has_snapshot(self) -> bool:
        return bool(self.execute("SELECT 1 FROM snapshots LIMIT 1"))

    def items(
        self,
        offset: int = 0,
        limit: int = 50,
        queue: str | None = None,
        *,
        after: int | None = None,
        include_removed: bool = False,
        removed_only: bool = False,
    ) -> list[StoredItem]:
        """A page of items in snapshot order. ``after`` = last ordinal seen (keyset paging)."""
        limit = max(0, min(int(limit), 10_000))
        where, params = self._where(queue, include_removed, removed_only)
        if after is not None:
            where.append("ordinal > ?")
            params.append(int(after))
        sql = f"SELECT {_ITEM_COLS} FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ordinal LIMIT ? OFFSET ?"
        params += [limit, max(0, int(offset)) if after is None else 0]
        return [_item(r) for r in self.execute(sql, params)]

    def _where(self, queue, include_removed, removed_only) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if removed_only:
            where.append("removed = 1")
        elif not include_removed:
            where.append("removed = 0")
        if queue is not None:
            where.append("queue = ?")
            params.append(queue)
        return where, params

    def iter_items(
        self, batch_size: int = 1000, queue: str | None = None, include_removed: bool = False
    ) -> Iterator[StoredItem]:
        """Every item, streamed page by page (keyset)."""
        after = -1
        while True:
            page = self.items(
                limit=batch_size, queue=queue, after=after, include_removed=include_removed
            )
            if not page:
                return
            yield from page
            after = page[-1].ordinal

    def item(self, item_id: str) -> StoredItem | None:
        rows = self.execute(f"SELECT {_ITEM_COLS} FROM items WHERE item_id = ?", (item_id,))
        return _item(rows[0]) if rows else None

    def count(
        self, queue: str | None = None, *, include_removed: bool = False, removed_only: bool = False
    ) -> int:
        where, params = self._where(queue, include_removed, removed_only)
        sql = "SELECT count(*) FROM items" + (" WHERE " + " AND ".join(where) if where else "")
        return int(self.execute(sql, params)[0][0])

    def queues(self) -> list[tuple[str, int]]:
        """(queue, active item count), in first-seen (snapshot) order."""
        rows = self.execute(
            "SELECT queue, count(*), min(ordinal) FROM items WHERE removed = 0 "
            "GROUP BY queue ORDER BY 3"
        )
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------ labels

    def append_label(self, record: LabelRecord, project_version: int | None = None) -> None:
        body = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO labels (record_id, item_id, labeller, project_version, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (record.record_id, record.item_id, record.labeller, project_version, body),
            )

    def labels(
        self,
        item_id: str | None = None,
        labeller: str | None = None,
        *,
        offset: int = 0,
        limit: int | None = None,
        since_seq: int | None = None,
    ) -> list[LabelRecord]:
        return [rec for _, rec, _ in self.label_rows(item_id, labeller, offset, limit, since_seq)]

    def label_rows(
        self,
        item_id: str | None = None,
        labeller: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        since_seq: int | None = None,
    ) -> list[tuple[int, LabelRecord, int | None]]:
        """(seq, record, project_version) in log order."""
        where, params = [], []
        if item_id is not None:
            where.append("item_id = ?")
            params.append(item_id)
        if labeller is not None:
            where.append("labeller = ?")
            params.append(labeller)
        if since_seq is not None:
            where.append("seq > ?")
            params.append(since_seq)
        sql = "SELECT seq, body, project_version FROM labels"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq LIMIT ? OFFSET ?"
        params += [-1 if limit is None else int(limit), max(0, int(offset))]
        return [
            (r[0], LabelRecord.model_validate_json(r[1]), r[2]) for r in self.execute(sql, params)
        ]

    def iter_labels(self, batch_size: int = 1000) -> Iterator[tuple[LabelRecord, int | None]]:
        """Every label record with its project_version, streamed (for exports)."""
        seq = 0
        while True:
            rows = self.label_rows(since_seq=seq, limit=batch_size)
            if not rows:
                return
            for s, rec, ver in rows:
                yield rec, ver
                seq = s

    def count_labels(self) -> int:
        return int(self.execute("SELECT count(*) FROM labels")[0][0])

    # LabelStore protocol (state.Session) --------------------------------------------
    def load(self) -> list[LabelRecord]:
        return self.labels()

    def append(self, record: LabelRecord) -> None:
        self.append_label(record, self._version())

    def _version(self) -> int | None:
        v = self.meta("project_version")
        return int(v) if v is not None else None

    def set_project_version(self, version: int) -> None:
        """The version ``append`` stamps on labels (dev 3's context sets it on reload)."""
        self.set_meta("project_version", str(int(version)))

    # ------------------------------------------------------------------ snapshots

    def snapshots(self, offset: int = 0, limit: int = 50) -> list[SnapshotInfo]:
        rows = self.execute(
            "SELECT id, taken_at, kind, n_items, n_new, n_removed, n_changed, note "
            "FROM snapshots ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
        return [
            SnapshotInfo(
                id=r[0],
                taken_at=r[1],
                kind=r[2],
                n_items=r[3],
                n_new=r[4],
                n_removed=r[5],
                n_changed=r[6],
                note=r[7],
            )
            for r in rows
        ]

    def latest_snapshot(self) -> SnapshotInfo | None:
        s = self.snapshots(limit=1)
        return s[0] if s else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
