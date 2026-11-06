"""Usability #9 / #11 / #20: export columns that mean something, nested JSON lines, local names.

Golden and rollouts keep their encoding (``caliper eval`` / ``caliper kappa``): Unsure is
still ``human_label: 0`` there. Only the label table (csv / jsonl / parquet / table) changes.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # --import-mode=importlib: find loop_helpers
from loop_helpers import ROWS, client_for, make_ctx, post

PRIORITY = {
    "type": "single",
    "options": [
        {"value": "low", "label": "Low", "hotkey": "1"},
        {"value": "medium", "label": "Medium", "hotkey": "2"},
        {"value": "high", "label": "High", "hotkey": "3"},
    ],
}


def _jsonl(c, q: str = "") -> list[dict]:
    r = c.get(f"/api/export/download?format=jsonl{q}")
    assert r.status_code == 200, r.text
    return [json.loads(x) for x in r.text.splitlines()]


def test_unsure_is_null_in_the_table_but_rollouts_keep_their_encoding(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    post(c, "t0", "correct")
    post(c, "t1", "wrong")
    post(c, "t2", "unsure", note="mixed review")
    rows = {r["item_id"]: r for r in _jsonl(c)}
    assert (rows["t0"]["approved"], rows["t0"]["human_label"]) == (True, 1)
    assert (rows["t1"]["approved"], rows["t1"]["human_label"]) == (False, 0)
    assert (rows["t2"]["approved"], rows["t2"]["human_label"]) == (None, None)
    # rollouts (caliper kappa) and golden-style scores are unchanged: Unsure stays 0
    roll = c.get("/api/export/download?format=rollouts").text.splitlines()
    hl = {json.loads(x)["id"]: json.loads(x)["rollout"]["metadata"]["human_label"] for x in roll}
    assert hl == {"t0": 1, "t1": 0, "t2": 0}


def test_a_scored_unsure_keeps_its_score(tmp_path: Path) -> None:
    opts = [
        {"value": "correct", "label": "Correct", "hotkey": "1", "approved": True},
        {"value": "wrong", "label": "Wrong", "hotkey": "2"},
        {"value": "unsure", "label": "Unsure", "hotkey": "3", "score": 0.5},
    ]
    c = client_for(make_ctx(tmp_path, labels={"type": "single", "options": opts}))
    post(c, "t0", "unsure")
    assert _jsonl(c)[0]["human_label"] == 0.5


def test_no_approved_option_means_no_approved_columns(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path, labels=PRIORITY))
    post(c, "t0", "high")
    post(c, "t1", "low")
    for row in _jsonl(c):
        assert "approved" not in row and "human_label" not in row
    text = c.get("/api/export/download?format=csv").text
    header = next(csv.reader(io.StringIO(text)))
    assert "approved" not in header and "human_label" not in header and "label" in header


def test_jsonl_nests_the_item_and_parses_json_columns_csv_stays_flat(tmp_path: Path) -> None:
    rows = [{**r, "meta": json.dumps({"channel": "email", "n": i})} for i, r in enumerate(ROWS)]
    card = {
        "fields": [
            {"column": "subject"},
            {"column": "meta", "type": "json"},
            {"column": "score", "type": "number"},
        ]
    }
    c = client_for(make_ctx(tmp_path, rows=rows, card=card))
    post(c, "t3", "correct")
    (row,) = _jsonl(c)
    assert not any(k.startswith("item.") for k in row)
    assert row["item"]["subject"] == "Fictional ticket 3"
    assert row["item"]["meta"] == {"channel": "email", "n": 3}  # an object, not JSON text
    assert row["item"]["score"] == 0.3
    # one row per label nests too
    (per_label,) = _jsonl(c, "&rows=labels")
    assert per_label["item"]["meta"]["channel"] == "email"
    # CSV stays flat, JSON as text
    flat = next(csv.DictReader(io.StringIO(c.get("/api/export/download?format=csv").text)))
    assert flat["item.subject"] == "Fictional ticket 3"
    assert json.loads(flat["item.meta"]) == {"channel": "email", "n": 3}


def test_file_names_use_local_time_with_the_offset(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    post(c, "t0", "correct")
    cd = c.get("/api/export/download?format=csv").headers["content-disposition"]
    name = re.search(r'filename="([^"]+)"', cd).group(1)
    assert re.fullmatch(r"ticket-triage-fictional-csv-\d{8}-\d{6}(Z|[+-]\d{4})\.csv", name), name
    r = c.post("/api/export", json={"format": "jsonl", "destination": {"kind": "file"}}).json()
    assert re.search(r"-jsonl-\d{8}-\d{6}(Z|[+-]\d{4})\.jsonl$", r["destination"])


def test_formats_have_plain_titles(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    fm = {f["name"]: f for f in c.get("/api/export/formats").json()["formats"]}
    assert fm["golden"]["title"] == "Answer key for evaluations"
    assert "caliper eval" in fm["golden"]["help"]  # the technical detail is still there
    assert all(f["title"] and f["plain"] for f in fm.values())
