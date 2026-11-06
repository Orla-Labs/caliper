"""Regression tests for the setup-side fixes from usability test 1 (lpf-a)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from caliper_label.project import CardField, DisplayType, load_project
from caliper_label.project_setup import default_display
from caliper_label.typedetect import detect_display

# --------------------------------------------------------------------------- #15 datetime


@pytest.mark.parametrize(
    ("dtype", "values"),
    [
        ("TIMESTAMP", ["2026-11-06 09:30:00"]),
        ("TIMESTAMP WITH TIME ZONE", ["2026-11-06T09:30:00+00:00"]),
        ("TIMESTAMPTZ", ["2026-11-06T09:30:00Z"]),
        ("TIMESTAMP_NS", [None]),
        ("DATE", ["2026-11-06"]),
        ("VARCHAR", ["2026-11-06T09:30:00Z", "2026-11-05T17:02:11.123+01:00"]),
        ("VARCHAR", ["2026-11-06", "2026-10-30"]),
        ("VARCHAR", ["2026-11-06 09:30", "2026-11-06 10:00:59"]),
        ("unknown", [datetime(2026, 11, 6, tzinfo=UTC), date(2026, 11, 5)]),
    ],
)
def test_detect_datetime(dtype: str, values: list[Any]) -> None:
    assert detect_display(dtype, values) is DisplayType.DATETIME
    assert default_display(dtype, values) is DisplayType.DATETIME


@pytest.mark.parametrize(
    "values",
    [["2026-11-06 and more"], ["11/06/2026"], ["20261106"], ["2026-11-06", "not a date"]],
)
def test_detect_datetime_needs_every_value_iso(values: list[str]) -> None:
    assert detect_display("VARCHAR", values) is not DisplayType.DATETIME


def test_datetime_field_round_trips(tmp_path) -> None:
    (tmp_path / "caliper.label.yaml").write_text(
        "version: 1\nname: t\nsource: {kind: file, path: d.csv}\n"
        "card:\n  fields:\n    - {column: created_at, type: datetime}\n"
        "labels:\n  options: [{value: a}, {value: b}]\n"
    )
    cfg = load_project(tmp_path)
    assert cfg.card.fields[0] == CardField(column="created_at", type=DisplayType.DATETIME)
