"""Dev 2's routers inside dev 3's real app: start screen -> wizard -> labelling -> settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from caliper_label.app import create_app
from fastapi.testclient import TestClient

CSV = "ticket_id,subject,priority\n" + "".join(f"t{i},Subject {i},p{i % 2}\n" for i in range(10))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CALIPER_LABEL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    app = create_app(ctx=None)
    with TestClient(app) as c:
        yield c
    ctx = app.state.ctx
    if ctx is not None and getattr(ctx, "store", None) is not None:
        ctx.store.close()


def _config(path: str) -> dict:
    return {
        "name": "Ticket triage",
        "source": {"kind": "file", "path": path, "id_column": "ticket_id"},
        "card": {"fields": [{"column": "subject"}, {"column": "priority", "type": "badge"}]},
        "labels": {
            "options": [
                {"value": "correct", "label": "Correct", "hotkey": "1", "approved": True},
                {"value": "wrong", "label": "Wrong", "hotkey": "2"},
                {"value": "unsure", "label": "Unsure", "hotkey": "3"},
            ]
        },
        "workflow": {"queues": {"by": "priority"}},
    }


def test_wizard_to_first_label_to_settings(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/status").json()["project"] is None
    up = client.put("/api/setup/upload?filename=t.csv", content=CSV.encode()).json()
    pv = client.post("/api/setup/preview", json={"source": {"kind": "file", "path": up["path"]},
                                                "draft": up["draft"]}).json()  # fmt: skip
    assert pv["ok"] and pv["count"] == 10
    body = {"dir": "triage", "config": _config(up["path"]), "draft": up["draft"]}
    r = client.post("/api/setup/create", json=body)
    assert r.status_code == 200, r.text
    status = client.get("/api/status").json()
    assert status["project"]["name"] == "Ticket triage"

    nxt = client.get("/api/next", params={"labeller": "ana"}).json()
    item = nxt["item"]
    assert item["id"].startswith("t")
    lab = {"item_id": item["id"], "labeller": "ana", "decision": "unsure"}
    r = client.post("/api/labels", json=lab)
    assert r.status_code == 200, r.text

    s = client.get("/api/settings").json()
    assert s["label_counts"] == {"unsure": 1}
    cfg = s["config"]
    cfg["labels"]["options"] = cfg["labels"]["options"][:2]
    cfg["labels"]["options"][1]["label"] = "Wrong answer"
    r = client.put("/api/settings", json={"config": cfg, "revision": 1, "confirm": True})
    assert r.status_code == 200, r.text
    assert client.app.state.ctx.version == 2
    assert client.get("/api/status").json()["project"]["version"] == 2
    # the retired option can no longer be chosen; its label is kept
    nxt = client.get("/api/next", params={"labeller": "ana"}).json()["item"]
    r = client.post("/api/labels", json={**lab, "item_id": nxt["id"]})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["label_counts"] == {"unsure": 1}


def test_demo_and_open_through_real_context(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/projects/demo", json={})
    assert r.status_code == 200, r.text
    assert client.get("/api/status").json()["project"]["name"].startswith("Catalog cleanup")
    item = client.get("/api/next", params={"labeller": "ana"}).json()["item"]
    assert item["id"].startswith("lst-")
    r = client.post("/api/projects/open", json={"path": str(tmp_path)})
    assert r.status_code == 404 and r.json()["hint"]


def test_team_mode_is_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIPER_LABEL_HOME", str(tmp_path / "home"))
    app = create_app(ctx=None, auth_token="tok")
    remote = TestClient(app, client=("203.0.113.9", 5000))
    r = remote.get("/api/setup/browse", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403 and r.json()["hint"]


def test_settings_card_change_then_refresh(client: TestClient, tmp_path: Path) -> None:
    up = client.put("/api/setup/upload?filename=t.csv", content=CSV.encode()).json()
    body = {"dir": "p", "config": _config(up["path"]), "draft": up["draft"]}
    assert client.post("/api/setup/create", json=body).status_code == 200
    cfg = client.get("/api/settings").json()["config"]
    cfg["card"]["fields"].append({"column": "ticket_id", "type": "text"})
    r = client.put("/api/settings", json={"config": cfg, "revision": 1})
    assert r.status_code == 200 and r.json()["needs_refresh"] is True
    d = client.post("/api/source/refresh").json()
    assert d["n_changed"] == 10 and d["n_new"] == 0
    r = client.post("/api/source/refresh/apply", json={"diff_id": d["diff_id"]})
    assert r.status_code == 200
    assert client.post("/api/source/refresh").json()["n_changed"] == 0
    # a source change re-opens the connector: refresh now reads the new file
    (tmp_path / "p" / "data" / "more.csv").write_text(CSV + "t99,Subject 99,p1\n")
    cfg = client.get("/api/settings").json()["config"]
    cfg["source"]["path"] = "data/more.csv"
    r = client.put("/api/settings", json={"config": cfg, "revision": 2})
    assert r.status_code == 200 and r.json()["needs_refresh"] is True
    assert client.post("/api/source/refresh").json()["n_new"] == 1
