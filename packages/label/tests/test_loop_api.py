"""Project-mode REST API and labelling loop (dev 3): items, labels, next, types, golden."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from caliper_label.state import Session

sys.path.insert(0, str(Path(__file__).parent))  # --import-mode=importlib: find loop_helpers
from loop_helpers import ROWS, client_for, make_ctx, next_ids, post


def test_status_and_no_project(tmp_path: Path) -> None:
    c = client_for(None)
    assert c.get("/api/status").json() == {
        "auth": "none",
        "authed": True,
        "mode": "none",
        "project": None,
    }
    r = c.get("/api/next?labeller=ana")
    assert r.status_code == 409
    assert r.json()["detail"] == "no project open" and "caliper label" in r.json()["hint"]
    c = client_for(make_ctx(tmp_path))
    st = c.get("/api/status").json()
    assert st["mode"] == "project" and st["project"]["version"] == 3


def test_errors_are_detail_and_hint(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    r = c.post("/api/labels", json={"item_id": "t0"})  # no labeller
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str) and "labeller" in body["detail"] and body["hint"]
    r = c.get("/api/nope")
    assert r.status_code == 404 and set(r.json()) == {"detail", "hint"}
    r = c.get("/api/items/zzz")
    assert r.status_code == 404 and r.json()["detail"] == "unknown item 'zzz'"


def test_items_paginated_and_one_item(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    page = c.get("/api/items?offset=2&limit=3").json()
    assert page["total"] == 12 and [i["id"] for i in page["items"]] == ["t2", "t3", "t4"]
    high = c.get("/api/items?queue=high&limit=100").json()
    assert high["total"] == 4 and all(i["queue"] == "high" for i in high["items"])
    one = c.get("/api/items/t1").json()["item"]
    fields = {f["label"]: f for f in one["fields"]}
    assert fields["subject"]["value"] == "Fictional ticket 1"
    assert fields["body"]["type"] == "long_text"
    assert fields["score"]["value"] == "0.1"
    assert one["category"] == "shipping" and one["queue"] == "low"


def test_post_and_fetch_labels_carry_version(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    r = post(c, "t0", "correct", note="clear case")
    assert r.status_code == 200
    rec = r.json()["record"]
    assert rec["labeller"] == "ana" and rec["stage"] == "triage" and rec["project_version"] == 3
    assert rec["note"] == "clear case" and rec["labelled_at"].endswith("Z")
    assert post(c, "t0", "wrong").status_code == 409  # already labelled: undo first
    got = c.get("/api/labels?item_id=t0").json()
    assert got["total"] == 1 and got["labels"][0]["decision"] == "correct"
    c.post("/api/undo", json={"labeller": "ana"})
    assert c.get("/api/labels?item_id=t0").json()["total"] == 0
    assert c.get("/api/labels?item_id=t0&all=true").json()["total"] == 2


def test_next_prefetch_is_distinct_and_leased(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    ana = next_ids(c, "ana", 5)
    assert ana == ["t0", "t1", "t2", "t3", "t4"]
    ben = next_ids(c, "ben", 5)
    assert not set(ana) & set(ben)  # leases keep two labellers apart
    # refilling after a label returns the rest plus one more, in order
    post(c, "t0", "correct")
    assert next_ids(c, "ana", 5) == ["t1", "t2", "t3", "t4", "t10"]


def test_next_done_reasons(tmp_path: Path) -> None:
    rows = ROWS[:3]  # t0 high, t1 low, t2 low
    c = client_for(make_ctx(tmp_path, rows=rows))
    post(c, "t0", "correct")
    body = c.get("/api/next?labeller=ana&queue=high").json()
    assert body["done"] and body["reason"] == "queue_done"
    post(c, "t1", "wrong")
    post(c, "t2", "wrong")
    body = c.get("/api/next?labeller=ana").json()
    assert body["done"] and body["reason"] == "all_done" and body["items"] == []
    empty = client_for(make_ctx(tmp_path, rows=[]))
    assert empty.get("/api/next?labeller=ana").json()["reason"] == "empty"


def test_requires_note_and_retired_option(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        labels={
            "type": "single",
            "options": [
                {"value": "correct", "hotkey": "1"},
                {"value": "unsure", "hotkey": "3", "requires_note": True},
                {"value": "old", "retired": True},
            ],
        },
    )
    c = client_for(ctx)
    r = post(c, "t0", "unsure")
    assert r.status_code == 422 and "needs a note" in r.json()["detail"] and r.json()["hint"]
    assert post(c, "t0", "unsure", note="two codes fit").status_code == 200
    r = post(c, "t1", "old")
    assert r.status_code == 400 and "retired" in r.json()["detail"]
    cfg = c.get("/api/label-config").json()
    assert [o["value"] for o in cfg["options"] if o["retired"]] == ["old"]


def test_label_types(tmp_path: Path) -> None:
    multi = client_for(
        make_ctx(
            tmp_path,
            labels={"type": "multi", "options": [{"value": "a"}, {"value": "b"}, {"value": "c"}]},
        )
    )
    r = post(multi, "t0", ["c", "a", "a"])
    assert r.status_code == 200 and r.json()["record"]["decision"] == '["a", "c"]'
    assert post(multi, "t1", []).status_code == 400
    assert post(multi, "t1", ["zz"]).status_code == 400
    hot = [o["hotkey"] for o in multi.get("/api/label-config").json()["options"]]
    assert hot == ["1", "2", "3"]  # auto-assigned

    ynu = client_for(make_ctx(tmp_path, labels={"type": "yes_no_unsure", "options": []}))
    cfg = ynu.get("/api/label-config").json()
    assert [o["value"] for o in cfg["options"]] == ["yes", "no", "unsure"]
    assert post(ynu, "t0", "yes").status_code == 200

    text = client_for(make_ctx(tmp_path, labels={"type": "free_text", "options": []}))
    assert post(text, "t0", "   ").status_code == 400
    assert post(text, "t0", " Refund issued ").json()["record"]["decision"] == "Refund issued"

    score = client_for(
        make_ctx(tmp_path, labels={"type": "score", "score": {"min": 1, "max": 5, "step": 1}})
    )
    assert post(score, "t0", 4).json()["record"]["decision"] == "4"
    assert post(score, "t1", "4.0").json()["record"]["decision"] == "4"
    assert post(score, "t2", 6).status_code == 400
    assert post(score, "t2", 2.5).status_code == 400  # off the step grid
    half = client_for(
        make_ctx(tmp_path, labels={"type": "score", "score": {"min": 0, "max": 1, "step": 0.5}})
    )
    assert post(half, "t0", 0.5).json()["record"]["decision"] == "0.5"


def test_golden_checks_hidden_and_scored(tmp_path: Path) -> None:
    rows = [dict(r) for r in ROWS]
    rows[5]["known"] = "correct"
    rows[7]["known"] = "Wrong"  # by label, case-insensitive
    ctx = make_ctx(
        tmp_path,
        rows=rows,
        card={"fields": [{"column": "subject"}, {"column": "known"}]},
        workflow={"golden": {"answer_column": "known", "rate": 1.0}},
    )
    c = client_for(ctx)
    body = c.get("/api/next?labeller=ana&n=4").json()
    ids = [i["id"] for i in body["items"]]
    assert set(ids[:2]) == {"t5", "t7"}  # rate 1.0: golden first, each once
    blob = json.dumps(body)
    assert "known" not in blob and "Wrong" not in blob  # the answer never reaches the browser
    assert all(i["labels_so_far"] == 0 for i in body["items"])
    post(c, "t5", "correct")
    post(c, "t7", "correct")  # wrong answer
    post(c, "t5", "wrong", who="ben")
    dash = c.get("/api/dashboard").json()
    acc = {x["name"]: x["golden"] for x in dash["labellers"]}
    assert acc["ana"] == {"answered": 2, "correct": 1, "accuracy": 0.5}
    assert acc["ben"]["accuracy"] == 0.0
    assert dash["totals"]["golden_items"] == 2
    # golden items are a test, not data: no plain-flow serving, no export rows
    assert all(x["labels"] == 0 for x in dash["labellers"])
    csv = c.get("/api/export/download?format=jsonl").text
    assert csv == ""


def test_golden_rate_mixes_in(tmp_path: Path) -> None:
    rows = [dict(r, ticket_id=f"t{i}") for i, r in enumerate(ROWS * 5)]
    for r in rows[:10]:
        r["known"] = "correct"
    ctx = make_ctx(
        tmp_path, rows=rows, workflow={"golden": {"answer_column": "known", "rate": 0.2}}
    )
    s: Session = ctx.session
    golden = set(ctx.items.golden_ids())
    served = []
    for _ in range(40):
        it = s.next_item("ana")
        assert it is not None
        served.append(it.id in golden)
        s.label(it.id, "ana", "correct")
    assert 3 <= sum(served) <= 14  # about 20%, seeded per labeller
    assert served != [True] * 8 + [False] * 32  # spread out, not front-loaded


def test_round_robin_assignment(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, workflow={"assignment": "round_robin"})
    c = client_for(ctx)
    next_ids(c, "ana")  # join order: ana, ben
    next_ids(c, "ben")
    ana = next_ids(c, "ana", 20)
    ben = next_ids(c, "ben", 20)
    assert ana and ben and not set(ana) & set(ben)
    assert set(ana) | set(ben) == {r["ticket_id"] for r in ROWS}
    assert c.get("/api/label-config").json()["assignment"] == "round_robin"


def test_double_label_overlap_first(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path, workflow={"double_label": 2}))
    post(c, "t3", "correct", who="ana")
    assert next_ids(c, "ben", 2)[0] == "t3"  # finish the overlap first
    assert "t3" not in next_ids(c, "ana", 12)  # never twice for the same person


def test_openapi_documents_the_public_api(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path))
    spec = c.get("/openapi.json").json()
    paths = spec["paths"]
    for p in ("/api/items", "/api/items/{item_id}", "/api/labels", "/api/next"):
        assert p in paths, p
    assert "post" in paths["/api/labels"] and "get" in paths["/api/labels"]
    assert {t["name"] for t in spec["tags"]} >= {"items", "labels", "team", "export"}
    assert "/api/label" not in paths  # legacy aliases stay out of the docs
    assert c.get("/docs").status_code == 200


def test_reload_bumps_version_and_options(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    c = client_for(ctx)
    post(c, "t0", "correct")
    new = ctx.config.model_copy(deep=True)
    new.labels.options[1].retired = True
    ctx.reload(new)
    assert ctx.version == 4
    assert post(c, "t1", "wrong").status_code == 400
    assert post(c, "t1", "correct").json()["record"]["project_version"] == 4


def test_serving_reads_pages_not_everything(tmp_path: Path) -> None:
    rows = [dict(ROWS[0], ticket_id=f"r{i}", priority="all") for i in range(3000)]
    ctx = make_ctx(tmp_path, rows=rows, workflow={"queues": {"by": None}})
    calls: list[tuple[int, int]] = []
    orig = ctx.items.page

    def spy(offset: int, limit: int, queue: str | None = None):
        calls.append((offset, limit))
        return orig(offset, limit, queue)

    ctx.items.page = spy  # type: ignore[method-assign]
    s = ctx.session
    for _ in range(300):
        it = s.next_item("ana")
        s.label(it.id, "ana", "correct")
    assert all(limit <= 256 for _, limit in calls)
    calls.clear()
    s.next_items("ana", None, 5)
    assert len(calls) == 1 and calls[0][0] >= 256  # the cursor skips what is done


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/adjudications", "/api/export/summary"])
def test_team_routes_need_a_project(path: str) -> None:
    assert client_for(None).get(path).status_code == 409
