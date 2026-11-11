"""LabelRecord -> inter-rater kappa adapter: undo, skip, source and latest-wins rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from caliper_eval.inter_rater import (
    inter_rater_from_records,
    load_label_records,
    resolve_label_records,
)
from caliper_eval.types import LabelRecord, LabelSource


def _rec(
    rid: str,
    item: str,
    who: str,
    decision: str | None,
    *,
    at: str = "2026-11-06T12:00:00Z",
    source: LabelSource = LabelSource.INTERNAL_REVIEWER,
    category: str | None = None,
    skipped: bool = False,
    undoes: str | None = None,
) -> LabelRecord:
    return LabelRecord(
        record_id=rid,
        item_id=item,
        labeller=who,
        decision=decision,
        labelled_at=at,
        source=source,
        category=category,
        skipped=skipped,
        undoes=undoes,
    )


def test_resolution_rules() -> None:
    recs = [
        _rec("r1", "a", "ana", "keep"),
        _rec("r2", "a", "ben", "drop"),
        _rec("r3", "a", "ben", None, undoes="r2"),  # ben undoes his "drop"
        _rec("r4", "a", "ben", "keep", at="2026-11-06T12:05:00Z"),
        _rec("r5", "b", "ana", None, skipped=True),
        _rec("r6", "b", "ben", "drop"),
        _rec("r7", "b", "teacher", "drop", source=LabelSource.LLM_TEACHER),
        _rec("r8", "c", "ana", "drop", at="2026-11-06T13:00:00Z"),
        _rec("r9", "c", "ana", "keep", at="2026-11-06T12:00:00Z"),  # older: loses
        _rec("r10", "c", "ben", "drop"),
    ]
    got = [(r.item_id, r.labeller, r.decision) for r in resolve_label_records(recs)]
    assert got == [
        ("a", "ana", "keep"),
        ("a", "ben", "keep"),
        ("b", "ben", "drop"),
        ("c", "ana", "drop"),
        ("c", "ben", "drop"),
    ]
    with_teacher = resolve_label_records(recs, sources=list(LabelSource))
    assert ("b", "teacher") in {(r.item_id, r.labeller) for r in with_teacher}


def test_inter_rater_from_records_by_category(tmp_path: Path) -> None:
    recs = [
        _rec("1", "a", "ana", "keep", category="near_dup"),
        _rec("2", "a", "ben", "keep", category="near_dup"),
        _rec("3", "b", "ana", "keep", category="near_dup"),
        _rec("4", "b", "ben", "drop", category="near_dup"),
        _rec("5", "c", "ana", "drop", category="mention"),
        _rec("6", "c", "ben", "drop", category="mention"),
    ]
    rep = inter_rater_from_records(recs)
    assert rep.method == "cohen"
    assert rep.overall.n_items == 3
    assert rep.by_category["near_dup"].percent_all_agree == pytest.approx(0.5)
    assert rep.by_category["mention"].kappa_undefined

    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(r.model_dump_json() for r in recs) + "\n\n", encoding="utf-8")
    assert load_label_records(path) == recs


def test_load_label_records_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"item_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.jsonl:1"):
        load_label_records(path)
