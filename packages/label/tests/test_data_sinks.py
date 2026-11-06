"""Export sinks: files (csv/jsonl/parquet) and SQLite write-back (Postgres/MySQL/S3: -m docker)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import duckdb
import pytest
from caliper_label import sinks
from caliper_label.connectors import ConnectorError
from caliper_label.connectors.duck import load_extension
from caliper_label.project import SourceConfig


def has_sqlite_ext() -> bool:
    try:
        load_extension(duckdb.connect(), "sqlite")
        return True
    except ConnectorError:
        return False


def label_rows(n: int = 3, start: int = 0):
    for i in range(start, start + n):
        yield {
            "item_id": f"T-{i:03d}",
            "decision": "correct" if i % 2 else "wrong",
            "labeller": "ana",
            "labelled_at": "2026-11-06T12:00:00Z",
            "note": None if i else "first one",
            "project_version": 2,
            "fields": {"subject": f"Fictional {i}", "tags": ["a", "b"]},
        }


@pytest.mark.parametrize("fmt", ["csv", "jsonl", "parquet"])
def test_file_sinks_roundtrip(tmp_path: Path, fmt: str) -> None:
    res = sinks.write(
        label_rows(), SourceConfig(kind="file", path=f"out/labels.{fmt}"), project_dir=tmp_path
    )
    out = tmp_path / "out" / f"labels.{fmt}"
    assert res.rows == 3 and res.format == fmt and Path(res.target) == out
    if fmt == "jsonl":
        got = [json.loads(line) for line in out.read_text().splitlines()]
        assert got[0]["fields"] == {"subject": "Fictional 0", "tags": ["a", "b"]}
    elif fmt == "csv":
        got = list(csv.DictReader(out.open()))
        assert got[0]["item_id"] == "T-000" and json.loads(got[0]["fields"])["tags"] == ["a", "b"]
        assert list(got[0]) == [
            "item_id",
            "decision",
            "labeller",
            "labelled_at",
            "note",
            "project_version",
            "fields",
        ]
    else:
        got = duckdb.connect().execute(f"SELECT * FROM read_parquet('{out}')").fetchall()
        assert len(got) == 3 and got[0][0] == "T-000"
    with pytest.raises(ConnectorError) as e:
        sinks.write(label_rows(), SourceConfig(kind="file", path=str(out)))
    assert "overwrite" in (e.value.hint or "")
    assert (
        sinks.write(label_rows(1), SourceConfig(kind="file", path=str(out)), overwrite=True).rows
        == 1
    )
    assert not list((tmp_path / "out").glob(".caliper-export-*"))  # temp dirs cleaned


def test_file_sink_empty_and_bad_format(tmp_path: Path) -> None:
    assert sinks.write(iter([]), SourceConfig(kind="file", path=str(tmp_path / "e.csv"))).rows == 0
    assert (
        sinks.write(iter([]), SourceConfig(kind="file", path=str(tmp_path / "e.parquet"))).rows == 0
    )
    with pytest.raises(ConnectorError):
        sinks.write(label_rows(), SourceConfig(kind="file", path=str(tmp_path / "x.xlsx")))
    with pytest.raises(ConnectorError):
        sinks.write(label_rows(), SourceConfig(kind="http", path="https://e.invalid/x.csv"))


@pytest.mark.skipif(not has_sqlite_ext(), reason="DuckDB sqlite extension unavailable (offline)")
def test_sqlite_write_back_create_append_fail_replace(tmp_path: Path) -> None:
    target = SourceConfig(kind="sqlite", path="exports/review.db", table="labels")
    r1 = sinks.write(label_rows(3), target, project_dir=tmp_path)
    assert r1.created is True and r1.rows == 3 and r1.format == "table"
    r2 = sinks.write(label_rows(2, start=3), target, project_dir=tmp_path)
    assert r2.created is False
    db = sqlite3.connect(tmp_path / "exports" / "review.db")
    assert db.execute("SELECT count(*) FROM labels").fetchone()[0] == 5
    fields = db.execute("SELECT fields FROM labels WHERE item_id = 'T-004'").fetchone()[0]
    assert json.loads(fields)["subject"] == "Fictional 4"
    db.close()
    with pytest.raises(ConnectorError) as e:
        sinks.write(label_rows(1), target, project_dir=tmp_path, if_exists="fail")
    assert "replace" in (e.value.hint or "")
    r3 = sinks.write(label_rows(1), target, project_dir=tmp_path, if_exists="replace")
    assert r3.created is True
    db = sqlite3.connect(tmp_path / "exports" / "review.db")
    assert db.execute("SELECT count(*) FROM labels").fetchone()[0] == 1
