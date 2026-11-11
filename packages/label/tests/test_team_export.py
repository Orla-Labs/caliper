"""Dashboard, agreement, adjudication and export (dev 3)."""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

from caliper_eval.inter_rater import load_label_records
from caliper_eval.types import GoldenRow, LabelRecord
from caliper_sdk import EvalRow

sys.path.insert(0, str(Path(__file__).parent))  # --import-mode=importlib: find loop_helpers
from loop_helpers import ROWS, client_for, make_ctx, post

LABEL_COLS = {"labeller", "labelled_at", "note", "source", "stage", "project_version"}


def double(tmp_path: Path, **over):
    ctx = make_ctx(tmp_path, workflow={"double_label": 2, "queues": {"by": "priority"}}, **over)
    c = client_for(ctx)
    plan = {
        "t0": [("ana", "correct"), ("ben", "correct")],
        "t1": [("ana", "correct"), ("ben", "wrong")],
        "t2": [("ana", "wrong"), ("ben", "wrong")],
        "t3": [("ana", "wrong"), ("ben", "correct"), ("cy", "wrong")],
        "t4": [("ana", "correct")],
    }
    for iid, labels in plan.items():
        for who, dec in labels:
            assert post(c, iid, dec, who=who).status_code == 200
    return ctx, c


def test_dashboard(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    d = c.get("/api/dashboard").json()
    t = d["totals"]
    assert t["items"] == 12 and t["complete"] == 4 and t["labels"] == 10 and t["labellers"] == 3
    assert t["disagreements"] == 2 and t["open_disagreements"] == 2
    people = {x["name"]: x for x in d["labellers"]}
    assert people["ana"]["labels"] == 5 and people["ana"]["last_hour"] == 5
    assert people["ana"]["per_hour"] is None  # seconds of work: too little for a rate
    queues = {q["name"]: q for q in d["queues"]}
    assert queues["high"]["total"] == 4 and queues["high"]["last_hour"] >= 1
    agr = d["agreement"]
    assert agr["n_items_overlap"] == 4 and agr["overall"]["n_raters"] == 3
    cats = {x["category"] for x in agr["by_category"]}
    assert cats == {"billing", "shipping"}
    assert [x["item_id"] for x in d["disagreements"]] == ["t1", "t3"]


def test_adjudication_flow(tmp_path: Path) -> None:
    ctx, c = double(tmp_path)
    open_ = c.get("/api/adjudications").json()
    assert open_["counts"] == {"open": 2, "done": 0, "all": 2}
    first = open_["items"][0]
    assert first["item_id"] == "t1" and first["item"]["fields"]
    assert [x["labeller"] for x in first["labels"]] == ["ana", "ben"]
    assert first["labels"][1]["display"] == "Wrong"

    r = c.post(
        "/api/adjudications",
        json={"item_id": "t1", "adjudicator": "ana", "decision": "wrong", "note": "policy 4.2"},
    )
    assert r.status_code == 200 and r.json()["record"]["source"] == "adjudicated"
    # the adjudicator also labelled t1: their own vote is untouched
    assert ctx.session.state("t1").labels["ana"].decision == "correct"
    # undo never retracts an adjudication
    c.post("/api/undo", json={"labeller": "ana"})
    assert ctx.session.state("t1").adjudication is not None
    # a second adjudication replaces the first
    c.post(
        "/api/adjudications", json={"item_id": "t1", "adjudicator": "dee", "decision": "correct"}
    )
    assert ctx.session.state("t1").adjudication.labeller == "dee"
    lists = c.get("/api/adjudications?status=done").json()
    assert [x["item_id"] for x in lists["items"]] == ["t1"]
    assert lists["items"][0]["adjudication"]["adjudicator"] == "dee"
    assert c.get("/api/adjudications").json()["counts"]["open"] == 1
    # kappa: adjudications are not votes
    agr = c.get("/api/agreement").json()
    assert agr["overall"]["n_raters"] == 3

    rows = c.get("/api/export/download?format=jsonl").text.splitlines()
    by_id = {r["item_id"]: r for r in map(json.loads, rows)}
    assert by_id["t1"]["label"] == "correct" and by_id["t1"]["status"] == "adjudicated"
    assert by_id["t1"]["source"] == "adjudicated" and by_id["t1"]["labeller"] == "dee"
    assert "t3" not in by_id  # unresolved conflict, policy exclude
    bad = c.post("/api/adjudications", json={"item_id": "t1", "adjudicator": "x", "decision": "zz"})
    assert bad.status_code == 400


def test_export_summary_and_conflict_policy(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    s = c.get("/api/export/summary").json()
    assert (s["resolved"], s["conflicts"], s["incomplete"], s["unlabelled"]) == (2, 2, 1, 7)
    left = {x["item_id"]: x for x in s["left_out"]}
    assert left["t1"]["labels"] == {"ana": "Correct", "ben": "Wrong"}
    assert left["t4"] == {"item_id": "t4", "why": "incomplete", "labels_so_far": 1}
    m = c.get("/api/export/summary?conflicts=majority").json()
    assert m["resolved"] == 3 and m["conflicts"] == 1  # t3: 2 of 3 say wrong


def test_export_table_formats_carry_provenance(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    r = c.get("/api/export/download?format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["item_id"] for x in rows] == ["t0", "t2"]
    assert LABEL_COLS <= set(rows[0])
    t0 = rows[0]
    assert t0["label"] == "correct" and t0["approved"] == "True" and t0["human_label"] == "1"
    assert t0["labeller"] == "ana;ben" and t0["stage"] == "triage" and t0["project_version"] == "3"
    assert t0["item.subject"] == "Fictional ticket 0"

    per_label = c.get("/api/export/download?format=jsonl&rows=labels").text.splitlines()
    recs = [json.loads(x) for x in per_label]
    assert len(recs) == 10 and all(LABEL_COLS <= set(x) for x in recs)

    all_items = c.get("/api/export/download?format=jsonl&include_unlabelled=true").text
    statuses = {json.loads(x)["item_id"]: json.loads(x)["status"] for x in all_items.splitlines()}
    assert statuses == {
        "t0": "agreed",
        "t1": "conflict",
        "t2": "agreed",
        "t3": "conflict",
        "t4": "incomplete",
    }


def test_export_records_is_pure_labelrecord(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    text = c.get("/api/export/download?format=records").text
    p = tmp_path / "records.jsonl"
    p.write_text(text)
    recs = load_label_records(p)
    assert len(recs) == 10 and all(isinstance(r, LabelRecord) for r in recs)


def test_export_rollouts_and_golden_from_project(tmp_path: Path) -> None:
    rows = [
        dict(
            r,
            id=r["ticket_id"],
            request={"messages": [{"role": "user", "content": r["subject"]}]},
            reference_outcome="refund",
        )
        for r in ROWS
    ]
    ctx = make_ctx(
        tmp_path,
        rows=rows,
        card={
            "fields": [
                {"column": "subject"},
                {"column": "id", "hidden": True},
                {"column": "request", "type": "json", "hidden": True},
                {"column": "reference_outcome", "hidden": True},
            ]
        },
    )
    c = client_for(ctx)
    post(c, "t0", "correct")
    post(c, "t1", "wrong")
    roll = [
        EvalRow.model_validate_json(x)
        for x in c.get("/api/export/download?format=rollouts").text.splitlines()
    ]
    assert [(r.id, r.rollout.metadata["human_label"]) for r in roll] == [("t0", 1), ("t1", 0)]
    assert roll[0].rollout.metadata["project_version"] == 3
    assert roll[0].rollout.prompt.startswith("[user]")
    # golden needs GoldenRow-shaped items: display carries only card columns
    r = c.get("/api/export/download?format=golden")
    if r.status_code == 200:
        g = [GoldenRow.model_validate_json(x) for x in r.text.splitlines()]
        assert g[0].label == "correct" and g[0].label_approved is True
        assert g[0].metadata["labelling"]["project_version"] == 3
    else:
        assert r.status_code == 400 and r.json()["hint"]


def test_export_score_and_free_text(tmp_path: Path) -> None:
    sc = client_for(
        make_ctx(tmp_path, labels={"type": "score", "options": [], "score": {"min": 1, "max": 5}})
    )
    post(sc, "t0", 5)
    post(sc, "t1", 2)
    roll = [json.loads(x) for x in sc.get("/api/export/download?format=rollouts").text.splitlines()]
    assert [r["rollout"]["metadata"]["human_label"] for r in roll] == [1, 0.25]
    ft = client_for(make_ctx(tmp_path, labels={"type": "free_text", "options": []}))
    post(ft, "t0", "Refund issued")
    r = ft.get("/api/export/download?format=rollouts")
    assert r.status_code == 400 and "free-text" in r.json()["detail"]
    assert ft.get("/api/agreement").json()["overall"]["undefined_reason"].startswith("kappa is not")


def test_post_export_writes_inside_project_only(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    r = c.post(
        "/api/export",
        json={"format": "csv", "destination": {"kind": "file", "path": "exports/x.csv"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 2 and (tmp_path / "exports" / "x.csv").exists()
    r = c.post("/api/export", json={"format": "jsonl"})
    assert r.status_code == 200 and Path(r.json()["destination"]).parent == tmp_path / "exports"
    bad = c.post(
        "/api/export", json={"format": "csv", "destination": {"kind": "file", "path": "../out.csv"}}
    )
    assert bad.status_code == 403 and bad.json()["hint"]


def test_parquet_export_via_sinks(tmp_path: Path) -> None:
    _, c = double(tmp_path)
    r = c.post(
        "/api/export",
        json={"format": "parquet", "destination": {"kind": "file", "path": "exports/l.parquet"}},
    )
    assert r.status_code == 200, r.text
    import duckdb

    got = duckdb.sql(
        f"SELECT item_id, label, labeller FROM '{tmp_path / 'exports' / 'l.parquet'}' ORDER BY 1"
    ).fetchall()
    assert got == [("t0", "correct", "ana;ben"), ("t2", "wrong", "ana;ben")]
    d = c.get("/api/export/download?format=parquet")
    assert d.status_code == 200 and d.content[:4] == b"PAR1"


def test_throughput_counts_active_time_only() -> None:
    from datetime import datetime, timedelta

    from caliper_label.agreement import _rate

    t0 = datetime(2026, 11, 6, 9, 0)
    steady = [t0 + timedelta(seconds=30 * i) for i in range(21)]  # one label / 30 s
    assert _rate(steady) == 120.0
    with_break = steady + [
        steady[-1] + timedelta(hours=2) + timedelta(seconds=30 * i) for i in range(1, 3)
    ]
    assert 80 < _rate(with_break) < 90  # the 2-hour lunch counts as 5 minutes, not 2 hours
    assert _rate(steady[:2]) is None


def test_export_writes_back_to_a_sqlite_table(tmp_path: Path) -> None:
    import sqlite3

    _, c = double(tmp_path)
    dest = {"kind": "sqlite", "path": "out.db", "table": "triage_labels"}
    r = c.post("/api/export", json={"format": "table", "destination": dest})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 2 and r.json()["destination"] == "triage_labels"
    con = sqlite3.connect(tmp_path / "out.db")
    rows = con.execute(
        "SELECT item_id, label, labeller, project_version FROM triage_labels"
    ).fetchall()
    assert sorted(rows) == [("t0", "correct", "ana;ben", 3), ("t2", "wrong", "ana;ben", 3)]
