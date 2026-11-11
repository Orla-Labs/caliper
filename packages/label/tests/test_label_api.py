"""HTTP API: serving, labelling, skip, undo, queues, double labelling, images, agreement."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from caliper_eval.types import LabelSource
from caliper_label.store import load_records
from fastapi.testclient import TestClient


def nxt(c: TestClient, who: str = "ana", queue: str | None = None) -> str | None:
    q = f"&queue={queue}" if queue else ""
    body = c.get(f"/api/next?labeller={who}{q}").json()
    return None if body["done"] else body["item"]["id"]


def lab(c: TestClient, item: str, decision: str | None, who: str = "ana", **kw) -> dict:
    r = c.post("/api/label", json={"item_id": item, "labeller": who, "decision": decision, **kw})
    assert r.status_code == 200, r.text
    return r.json()["record"]


def test_index_and_config(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    assert "caliper label" in c.get("/").text
    assert c.get("/static/js/label/index.js").status_code == 200
    assert c.get("/static/css/tokens.css").status_code == 200
    cfg = c.get("/api/config").json()
    assert cfg["title"] == "Test project" and cfg["n_items"] == 5
    assert [d["key"] for d in cfg["decisions"]] == ["t", "f", "u"]
    assert cfg["queues"] == ["common", "rare"]
    assert cfg["keys"] == {"skip": "s", "undo": "backspace", "note": "n"}
    assert c.get("/api/nope").status_code == 404


def test_card_rendering(make_client: Callable[..., TestClient]) -> None:
    item = make_client().get("/api/next?labeller=ana").json()["item"]
    assert item["id"] == "g0" and item["queue"] == "common" and item["category"] == "CTX-100"
    f = {x["label"]: x for x in item["fields"]}
    assert f["Photo"]["src"] == "/api/image/g0/0"
    assert f["metadata.code"]["value"] == "CTX-100"
    assert f["metadata.score"]["value"] == "0.50"
    assert f["Listing"]["value"] == "Item: Moonwick widget 0"
    assert f["Open"]["href"] == "https://example.invalid/items/g0"


def test_label_advances_and_records_who_and_when(make_client, tmp_path: Path) -> None:
    c = make_client()
    rec = lab(c, "g0", "correct", note="  looks fine ")
    assert rec["labeller"] == "ana" and rec["labelled_at"].endswith("Z")
    assert rec["source"] == "internal_reviewer" and rec["stage"] == "triage"
    assert rec["queue"] == "common" and rec["category"] == "CTX-100"
    assert rec["note"] == "looks fine"
    assert nxt(c) == "g1"
    on_disk = load_records(tmp_path / "labels.jsonl")
    assert [r.record_id for r in on_disk] == [rec["record_id"]]


def test_validation_errors(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    bad = c.post("/api/label", json={"item_id": "g0", "labeller": "ana", "decision": "maybe"})
    assert bad.status_code == 400 and "unknown decision" in bad.json()["detail"]
    assert (
        c.post(
            "/api/label", json={"item_id": "zz", "labeller": "a", "decision": "correct"}
        ).status_code
        == 404
    )
    assert (
        c.post(
            "/api/label", json={"item_id": "g0", "labeller": " ", "decision": "correct"}
        ).status_code
        == 400
    )
    assert c.get("/api/next?labeller=%20").status_code == 400
    assert c.get("/api/next?labeller=ana&queue=nope").status_code == 404
    lab(c, "g0", "correct")
    again = c.post("/api/label", json={"item_id": "g0", "labeller": "ana", "decision": "wrong"})
    assert again.status_code == 409


def test_skip_comes_back_last_and_undo_restores(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    lab(c, "g0", None, skip=True)
    order = []
    for _ in range(4):
        iid = nxt(c)
        order.append(iid)
        lab(c, iid, "correct")
    assert order == ["g1", "g2", "g3", "g4"]
    assert nxt(c) == "g0"  # skipped item comes back once the rest is done
    payload = c.get("/api/next?labeller=ana").json()["item"]
    assert payload["skipped_before"] is True

    undo = c.post("/api/undo", json={"labeller": "ana"}).json()
    assert undo["undone"]["item_id"] == "g4" and undo["item"]["id"] == "g4"
    assert undo["record"]["undoes"] == undo["undone"]["record_id"]
    assert nxt(c) == "g4"  # undone item is available again
    lab(c, "g4", "wrong")  # relabel after undo is allowed
    # undo walks back: g4 (wrong), then g3, ...
    assert c.post("/api/undo", json={"labeller": "ana"}).json()["undone"]["decision"] == "wrong"
    assert c.post("/api/undo", json={"labeller": "ana"}).json()["undone"]["item_id"] == "g3"


def test_undo_is_per_labeller_and_noop_when_empty(make_client) -> None:
    c = make_client()
    assert c.post("/api/undo", json={"labeller": "ben"}).json()["undone"] is None
    lab(c, "g0", "correct", who="ana")
    assert c.post("/api/undo", json={"labeller": "ben"}).json()["undone"] is None
    assert c.post("/api/undo", json={"labeller": "ana"}).json()["undone"]["item_id"] == "g0"


def test_queues_progress(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    assert nxt(c, queue="rare") == "g2"
    lab(c, "g2", "correct")
    lab(c, "g0", "wrong")
    q = {x["name"]: x for x in c.get("/api/queues?labeller=ana").json()["queues"]}
    assert q["rare"] == {
        "name": "rare",
        "total": 2,
        "complete": 1,
        "mine": 1,
        "left": 1,
        "skipped": 0,
    }
    assert q["common"]["left"] == 2 and q["common"]["complete"] == 1
    lab(c, "g3", "correct")
    assert nxt(c, queue="rare") is None
    assert c.get("/api/queues").json()["labellers"] == {"ana": 3}


def test_single_label_mode_leases_avoid_duplicate_work(make_client) -> None:
    c = make_client()
    assert nxt(c, "ana") == "g0"
    assert nxt(c, "ben") == "g1"  # g0 is on ana's screen
    lab(c, "g1", "correct", who="ben")
    assert nxt(c, "ben") == "g2"


def test_double_labelling(make_client: Callable[..., TestClient]) -> None:
    c = make_client(double_label=2)
    assert nxt(c, "ana") == "g0"
    lab(c, "g0", "correct", who="ana")
    assert nxt(c, "ana") == "g1"
    # ben is served the item that already has one label first, then never sees it again
    assert nxt(c, "ben") == "g0"
    lab(c, "g0", "correct", who="ben")
    assert nxt(c, "ben") == "g1"
    assert c.get("/api/queues").json()["queues"][0]["complete"] == 1
    # a third labeller gets neither the complete g0 nor g1 (ana and ben both have it open)
    assert nxt(c, "cy") == "g2"
    # ana is never served g0 again
    seen = set()
    for _ in range(4):
        iid = nxt(c, "ana")
        seen.add(iid)
        lab(c, iid, "wrong", who="ana")
    assert "g0" not in seen and nxt(c, "ana") is None


def test_agreement_view(make_client: Callable[..., TestClient]) -> None:
    c = make_client(double_label=2)
    plan = {
        "g0": ("correct", "correct"),
        "g1": ("wrong", "wrong"),
        "g2": ("correct", "wrong"),
        "g3": ("wrong", "wrong"),
        "g4": ("correct", "correct"),
    }
    for iid, (a, b) in plan.items():
        lab(c, iid, a, who="ana")
        lab(c, iid, b, who="ben", note="hmm" if a != b else None)
    agr = c.get("/api/agreement").json()
    assert agr["method"] == "cohen" and agr["n_items_overlap"] == 5
    # p_o = 4/5; ana: correct 3, wrong 2; ben: correct 2, wrong 3; p_e = (6+6)/25
    assert agr["overall"]["kappa"] == pytest.approx((0.8 - 0.48) / 0.52)
    cats = {x["category"]: x for x in agr["by_category"]}
    assert set(cats) == {"CTX-100", "CTX-520", "CTX-210"}
    assert cats["CTX-100"]["n_items"] == 2 and cats["CTX-100"]["percent_all_agree"] == 1.0
    assert [d["item_id"] for d in agr["disagreements"]] == ["g2"]
    assert agr["disagreements"][0]["labels"][1]["note"] == "hmm"


def test_agreement_ignores_llm_teacher(make_client: Callable[..., TestClient]) -> None:
    c = make_client(double_label=2, source=LabelSource.LLM_TEACHER.value)
    for iid in ("g0", "g1"):
        lab(c, iid, "correct", who="ana")
        lab(c, iid, "wrong", who="gpt")
    assert c.get("/api/agreement").json()["n_items_overlap"] == 0


def test_sqlite_store_works_end_to_end(make_client, tmp_path: Path) -> None:
    c = make_client(store="labels.sqlite")
    lab(c, "g0", "correct")
    c.post("/api/undo", json={"labeller": "ana"})
    recs = load_records(tmp_path / "labels.sqlite")
    assert [r.undoes is None for r in recs] == [True, False]


def test_session_resumes_from_store(make_session, make_client) -> None:
    c = make_client()
    lab(c, "g0", "correct")
    lab(c, "g1", None, skip=True)
    c.app.state.session.store.close()
    again = make_client()
    assert nxt(again) == "g2"


# --------------------------------------------------------------------------- images


def test_image_served(make_client: Callable[..., TestClient]) -> None:
    r = make_client().get("/api/image/g0/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "img, status",
    [
        ("../secret.png", 403),
        ("img/../../secret.png", 403),
        ("/etc/hosts", 403),
        ("{abs}", 403),
        ("../secret.txt", 403),
        ("img/missing.png", 404),
        ("img/g0.txt", 403),
        ("https://example.invalid/a.png", 404),  # URLs go straight to the browser
    ],
)
def test_image_path_traversal_guard(make_client, data_dir: Path, img: str, status: int) -> None:
    img = img.replace("{abs}", str(data_dir.parent / "secret.png"))
    rows = [{"id": "x", "text": "t", "metadata": {"img": img}}]
    c = make_client(rows=rows, queue_key=None)
    assert c.get("/api/image/x/0").status_code == status


def test_image_symlink_escape_blocked(make_client, data_dir: Path) -> None:
    link = data_dir / "img" / "link.png"
    os.symlink(data_dir.parent / "secret.png", link)
    rows = [{"id": "x", "metadata": {"img": "img/link.png"}}]
    c = make_client(rows=rows, queue_key=None)
    assert c.get("/api/image/x/0").status_code == 403


def test_image_endpoint_only_serves_image_fields(make_client) -> None:
    c = make_client()
    assert c.get("/api/image/g0/1").status_code == 404  # a badge field
    assert c.get("/api/image/g0/99").status_code == 404
    assert c.get("/api/image/nope/0").status_code == 404


def test_url_images_pass_through(make_client) -> None:
    rows = [{"id": "x", "metadata": {"img": "https://example.invalid/a.png"}}]
    item = make_client(rows=rows, queue_key=None).get("/api/next?labeller=a").json()["item"]
    assert item["fields"][0]["src"] == "https://example.invalid/a.png"


def test_non_http_links_are_not_clickable(make_client) -> None:
    rows = [{"id": "javascript:alert(1)", "metadata": {}}]
    item = make_client(rows=rows, queue_key=None).get("/api/next?labeller=a").json()["item"]
    link = next(f for f in item["fields"] if f["type"] == "link")
    assert link["href"].startswith("https://")  # template prefix keeps it http(s)
    rows = [{"id": "y", "metadata": {"img": "javascript:alert(1)"}}]
    c = make_client(rows=rows, queue_key=None, fields=[{"key": "metadata.img", "type": "link"}])
    f = c.get("/api/next?labeller=a").json()["item"]["fields"][0]
    assert f["href"] is None and f["value"] == "javascript:alert(1)"
