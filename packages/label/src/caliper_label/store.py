"""Append-only storage for LabelRecords.

* ``labels.jsonl`` (default): one record per line, flushed and fsynced per write,
  so a crash loses at most the label being written. A torn last line is ignored
  on load (and reported).
* ``labels.sqlite``: the same records in one SQLite table (stdlib ``sqlite3``).

Nothing is ever rewritten or deleted. Undo appends a record whose ``undoes``
names the retracted record.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from caliper_eval.types import LabelRecord, canonical_json


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_record_id(fields: dict[str, object]) -> str:
    """sha256 over the record's fields plus a random nonce (two fast identical taps differ)."""
    payload = canonical_json({**fields, "_nonce": uuid.uuid4().hex})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class LabelStore(Protocol):
    path: Path

    def load(self) -> list[LabelRecord]: ...

    def append(self, record: LabelRecord) -> None: ...

    def close(self) -> None: ...


class JsonlStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def load(self) -> list[LabelRecord]:
        records: list[LabelRecord] = []
        if not self.path.exists():
            return records
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(LabelRecord.model_validate_json(line))
            except ValueError:
                if n == len(lines):  # torn final write: ignore, it never completed
                    print(
                        f"caliper label: ignoring incomplete last line in {self.path}",
                        file=sys.stderr,
                    )
                    continue
                raise ValueError(f"{self.path}:{n}: not a LabelRecord") from None
        return records

    def append(self, record: LabelRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS label_records ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            " record_id TEXT UNIQUE NOT NULL,"
            " item_id TEXT NOT NULL,"
            " body TEXT NOT NULL)"
        )

    def load(self) -> list[LabelRecord]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM label_records ORDER BY seq").fetchall()
        return [LabelRecord.model_validate_json(r[0]) for r in rows]

    def append(self, record: LabelRecord) -> None:
        body = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO label_records (record_id, item_id, body) VALUES (?, ?, ?)",
                (record.record_id, record.item_id, body),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_store(path: str | Path) -> LabelStore:
    suffix = Path(path).suffix.lower()
    if suffix in (".sqlite", ".sqlite3", ".db"):
        return SqliteStore(path)
    if suffix != ".jsonl":
        raise ValueError(f"store must end in .jsonl or .sqlite: {path}")
    return JsonlStore(path)


def load_records(path: str | Path) -> list[LabelRecord]:
    """Read a store without keeping it open (export, smoke)."""
    store = open_store(path)
    try:
        return store.load()
    finally:
        store.close()
