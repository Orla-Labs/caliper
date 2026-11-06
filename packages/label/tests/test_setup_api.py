"""Start screen, wizard and settings API (dev 2): routes_setup + routes_settings.

The app here is a bare FastAPI with both routers and a small `open_project` that builds the
context the way dev 3's `context.py` does (config, ProjectStore, connector, reload()).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from caliper_eval.types import LabelRecord
from caliper_label.connectors import open_connector
from caliper_label.project import load_project
from caliper_label.project_store import ProjectStore
from caliper_label.routes_settings import router as settings_router
from caliper_label.routes_setup import router as setup_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
CSV = "ticket_id,subject,body,priority,screenshot\n" + "".join(
    f"t{i},Subject {i},Body text for fictional ticket {i},p{i % 3 + 1},https://img.test/{i}.png\n"
    for i in range(30)
)


def _ctx(app: FastAPI, project_dir: Path) -> SimpleNamespace:
    cfg = load_project(project_dir)
    old = getattr(app.state, "ctx", None)
    if old is not None:
        old.store.close()
    ctx = SimpleNamespace(
        project_dir=project_dir,
        config=cfg,
        store=ProjectStore.open(project_dir),
        connector=open_connector(cfg.source, project_dir),
        version=cfg.revision,
    )

    def reload() -> None:
        ctx.config = load_project(project_dir)
        ctx.version = ctx.config.revision
        ctx.connector = open_connector(ctx.config.source, project_dir)

    ctx.reload = reload
    app.state.ctx = ctx
    return ctx


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CALIPER_LABEL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(setup_router)
    app.include_router(settings_router)
    app.state.ctx = None
    app.state.open_project = lambda d: _ctx(app, Path(d))
    with TestClient(app) as c:
        yield c
    if app.state.ctx is not None:
        app.state.ctx.store.close()


def _config(path: str = "data/tickets.csv", **over: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "name": "Ticket triage",
        "source": {"kind": "file", "path": path, "id_column": "ticket_id"},
        "card": {
            "fields": [
                {"column": "subject", "type": "text"},
                {"column": "body", "type": "long_text"},
                {"column": "priority", "type": "badge"},
            ]
        },
        "labels": {
            "type": "single",
            "options": [
                {"value": "correct", "label": "Correct", "hotkey": "1", "approved": True},
                {"value": "wrong", "label": "Wrong", "hotkey": "2"},
                {"value": "unsure", "label": "Unsure", "hotkey": "3", "requires_note": True},
            ],
        },
        "workflow": {"queues": {"by": "priority"}},
    }
    cfg.update(over)
    return cfg


def _upload(client: TestClient) -> dict[str, Any]:
    r = client.put("/api/setup/upload?filename=tickets.csv", content=CSV.encode())
    assert r.status_code == 200, r.text
    return r.json()


def _create(client: TestClient, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    up = _upload(client)
    r = client.post(
        "/api/setup/create",
        json={"dir": str(tmp_path / "proj"), "config": _config(up["path"]), "draft": up["draft"]},
    )
    assert r.status_code == 200, r.text
    return tmp_path / "proj", r.json()


# --------------------------------------------------------------------------- start screen


def test_projects_empty_and_demo_available(client: TestClient) -> None:
    r = client.get("/api/projects").json()
    assert r["recent"] == [] and r["demo_available"] is True


def test_demo_opens_a_copy_and_snapshots(client: TestClient, tmp_path: Path) -> None:
    demo_src = ROOT / "examples" / "label_demo"
    r = client.post("/api/projects/demo")
    assert r.status_code == 200, r.text
    proj = Path(r.json()["project"]["path"])
    assert proj == (tmp_path / "home" / "demo")
    ctx = client.app.state.ctx
    assert ctx.store.count() == 80
    assert not (demo_src / ".caliper-label").exists()  # examples/ stays clean
    text = (proj / "caliper.label.yaml").read_text()
    assert "# caliper label demo project" in text  # comments kept in the copy
    assert str(ROOT / "examples" / "catalog_cleanup" / "golden.jsonl") in text
    assert client.get("/api/projects").json()["recent"][0]["path"] == str(proj)
    # opening again reuses the copy (labels survive)
    assert client.post("/api/projects/demo").status_code == 200


def test_open_errors_and_forget(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/projects/open", json={"path": str(tmp_path)})
    assert r.status_code == 404
    body = r.json()
    assert "caliper.label.yaml" in body["detail"] and body["hint"]
    proj, _ = _create(client, tmp_path)
    r = client.post("/api/projects/open", json={"path": str(proj / "caliper.label.yaml")})
    assert r.status_code == 200 and r.json()["project"]["name"] == "Ticket triage"
    (proj / "caliper.label.yaml").write_text("source: {kind: nope}\n")
    r = client.post("/api/projects/open", json={"path": str(proj)})
    assert r.status_code == 422 and "source.kind" in r.json()["detail"]
    client.post("/api/projects/forget", json={"path": str(proj)})
    assert client.get("/api/projects").json()["recent"] == []


def test_open_without_opener_is_explained(client: TestClient, tmp_path: Path) -> None:
    proj, _ = _create(client, tmp_path)
    client.app.state.open_project = None
    r = client.post("/api/projects/open", json={"path": str(proj)})
    assert r.status_code == 501 and "caliper label" in r.json()["hint"]


# --------------------------------------------------------------------------- wizard


def test_upload_preview_create_snapshot(client: TestClient, tmp_path: Path) -> None:
    up = _upload(client)
    assert up["path"] == "data/tickets.csv" and up["size"] == len(CSV)
    pv = client.post(
        "/api/setup/preview",
        json={"source": {"kind": "file", "path": up["path"]}, "draft": up["draft"]},
    ).json()
    assert pv["ok"] is True, pv
    assert pv["count"] == 30 and len(pv["rows"]) == 30
    cols = {c["name"]: c for c in pv["columns"]}
    assert set(cols) == {"ticket_id", "subject", "body", "priority", "screenshot"}
    assert cols["screenshot"]["display"] == "image"
    assert pv["credentials"][0]["resolved"] is True

    r = client.post(
        "/api/setup/create",
        json={"dir": "proj", "config": _config(up["path"]), "draft": up["draft"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    proj = tmp_path / "proj"
    assert (proj / "data" / "tickets.csv").read_text() == CSV
    assert not (tmp_path / "home" / "drafts" / up["draft"]).exists()
    assert (proj / ".caliper-label" / ".gitignore").read_text() == "*\n"
    assert out["yaml"] == (proj / "caliper.label.yaml").read_text()
    assert load_project(proj).source.path == "data/tickets.csv"
    ctx = client.app.state.ctx
    assert ctx.store.count() == 30 and out["snapshot"] is not None
    assert {q for q, _n in ctx.store.queues()} == {"p1", "p2", "p3"}

    again = client.post("/api/setup/create", json={"dir": "proj", "config": _config()})
    assert again.status_code == 409 and again.json()["hint"]


def test_create_with_local_path_outside_project(client: TestClient, tmp_path: Path) -> None:
    data = tmp_path / "shared" / "t.csv"
    data.parent.mkdir()
    data.write_text(CSV)
    r = client.post("/api/setup/create", json={"dir": "p2", "config": _config(str(data))})
    assert r.status_code == 200, r.text
    assert load_project(tmp_path / "p2").source.path == str(data.resolve())
    inside = tmp_path / "p3" / "in.csv"
    inside.parent.mkdir()
    inside.write_text(CSV)
    r = client.post("/api/setup/create", json={"dir": "p3", "config": _config(str(inside))})
    assert load_project(tmp_path / "p3").source.path == "in.csv"


def test_create_refuses_invalid_config(client: TestClient) -> None:
    bad = _config()
    bad["labels"]["options"][1]["hotkey"] = "1"
    r = client.post("/api/setup/create", json={"dir": "x", "config": bad})
    assert r.status_code == 422
    assert "bound twice" in r.json()["detail"] and r.json()["hint"]
    r = client.post("/api/setup/create", json={"dir": "x", "config": {"source": {"kind": "ftp"}}})
    assert r.status_code == 422 and "source.kind" in r.json()["detail"]
    r = client.post("/api/setup/create", json={"config": {}})
    assert r.status_code == 422 and r.json()["detail"].startswith("dir")


def test_preview_errors_have_hints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pv = client.post("/api/setup/preview", json={"source": {"kind": "file", "path": "nope.csv"}})
    assert pv.status_code == 200
    body = pv.json()
    assert body["ok"] is False and body["error"]["hint"]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_URL", "postgresql://ana:s3cr3t-pw@127.0.0.1:1/x")
    pg = client.post(
        "/api/setup/preview",
        json={
            "source": {
                "kind": "postgres",
                "table": "t",
                "credentials": {"env": "APP_DB_URL"},
                "options": {"connect_timeout": 1},
            }
        },
    ).json()
    assert pg["ok"] is False
    assert "s3cr3t-pw" not in str(pg)  # never a value
    assert any(c["name"] == "APP_DB_URL" and c["resolved"] for c in pg["credentials"])
    r = client.post("/api/setup/preview", json={"source": {"kind": "ftp"}})
    assert r.status_code == 422 and r.json()["hint"]


def test_upload_rejects_bad_files(client: TestClient) -> None:
    r = client.put("/api/setup/upload?filename=evil.exe", content=b"MZ")
    assert r.status_code == 400 and r.json()["hint"]
    r = client.put("/api/setup/upload?filename=t.csv", content=b"")
    assert r.status_code == 400
    r = client.put("/api/setup/upload?filename=t.csv&draft=../../etc", content=b"a\n1\n")
    assert r.status_code == 400
    up = client.put("/api/setup/upload?filename=../../t.csv", content=b"a\n1\n").json()
    assert up["path"] == "data/t.csv"


def test_validate_and_templates(client: TestClient) -> None:
    v = client.post("/api/setup/validate", json={"config": _config()}).json()
    assert v["ok"] is True and v["yaml"].startswith("# caliper label project")
    bad = client.post("/api/setup/validate", json={"config": {"source": {}}}).json()
    assert bad["ok"] is False and bad["issues"][0]["path"] == "source.kind"
    ts = client.get("/api/setup/templates").json()["templates"]
    assert {t["id"] for t in ts} >= {"correct_wrong_unsure", "categories", "rating_1_5"}
    cats = client.post("/api/setup/templates/categories", json={"categories": "A\nB"}).json()
    assert [o["hotkey"] for o in cats["labels"]["options"]] == ["1", "2"]
    assert client.post("/api/setup/templates/nope").status_code == 404
    assert "file" in client.get("/api/setup/connectors").json()["connectors"]


def test_browse(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "x.csv").write_text("a\n")
    (tmp_path / "notes.txt").write_text("a\n")
    (tmp_path / ".hidden").mkdir()
    _create(client, tmp_path)
    b = client.get("/api/setup/browse", params={"path": str(tmp_path)}).json()
    kinds = {e["name"]: e["kind"] for e in b["entries"]}
    assert kinds["d"] == "dir" and kinds["x.csv"] == "file" and kinds["proj"] == "project"
    assert "notes.txt" not in kinds and ".hidden" not in kinds
    assert client.get("/api/setup/browse", params={"path": "/no/such"}).status_code == 404


def test_local_only_gate(client: TestClient) -> None:
    client.app.state.setup_local_only = True
    assert client.get("/api/setup/browse").status_code == 200  # TestClient counts as local

    remote = TestClient(client.app, client=("203.0.113.9", 5000))
    r = remote.get("/api/setup/browse")
    assert r.status_code == 403 and r.json()["hint"]
    assert remote.post("/api/setup/create", json={"dir": "x", "config": {}}).status_code == 403


# --------------------------------------------------------------------------- settings


def _label(ctx: Any, item: str, decision: str, who: str = "ana") -> None:
    rec = LabelRecord(
        item_id=item,
        decision=decision,
        labeller=who,
        labelled_at="2026-11-06T12:00:00Z",
        record_id=f"{item}-{who}-{decision}",
    )
    ctx.store.append_label(rec, ctx.version)


def test_settings_requires_project(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 409 and r.json()["detail"] == "no project open"


def test_settings_rename_retire_and_revision(client: TestClient, tmp_path: Path) -> None:
    proj, _ = _create(client, tmp_path)
    ctx = client.app.state.ctx
    _label(ctx, "t1", "unsure")
    _label(ctx, "t2", "correct")
    s = client.get("/api/settings").json()
    assert s["revision"] == 1 and s["label_counts"] == {"unsure": 1, "correct": 1}
    assert s["path"] == str(proj / "caliper.label.yaml")

    # hand comment, to prove settings edit in place
    f = proj / "caliper.label.yaml"
    f.write_text(f.read_text().replace("name: Ticket triage", "name: Ticket triage  # keep me"))

    cfg = s["config"]
    cfg["labels"]["options"][0]["label"] = "Right queue"  # rename
    cfg["labels"]["options"] = cfg["labels"]["options"][:2]  # remove "unsure" (has labels)
    plan = client.post("/api/settings/plan", json={"config": cfg}).json()
    assert plan["plan"]["needs_confirmation"] is True
    assert {(c["value"], c["kind"]) for c in plan["plan"]["changes"]} == {
        ("correct", "renamed"),
        ("unsure", "retired"),
    }
    assert "revision: 2" in plan["yaml"] and "# keep me" in plan["yaml"]

    r = client.put("/api/settings", json={"config": cfg, "revision": 1})
    assert r.status_code == 409 and "unsure" in r.json()["detail"] and r.json()["hint"]

    r = client.put("/api/settings", json={"config": cfg, "revision": 1, "confirm": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == 2 and client.app.state.ctx.version == 2
    opts = {o["value"]: o for o in out["config"]["labels"]["options"]}
    assert opts["correct"]["label"] == "Right queue"
    assert opts["unsure"]["retired"] is True and opts["unsure"]["hotkey"] is None
    assert out["label_counts"]["unsure"] == 1  # labels kept
    text = f.read_text()
    assert "# keep me" in text and "revision: 2" in text

    # stale revision
    r = client.put("/api/settings", json={"config": cfg, "revision": 1, "confirm": True})
    assert r.status_code == 409 and "reload" in r.json()["hint"]

    # UI -> YAML -> UI identical through the API
    again = client.get("/api/settings").json()
    assert again["config"] == out["config"]
    r = client.put("/api/settings", json={"config": again["config"], "revision": 2})
    assert r.status_code == 200 and r.json()["config"] == {**again["config"], "revision": 3}


def test_settings_rejects_invalid(client: TestClient, tmp_path: Path) -> None:
    _create(client, tmp_path)
    cfg = client.get("/api/settings").json()["config"]
    cfg["labels"]["options"][0]["hotkey"] = "z"
    r = client.put("/api/settings", json={"config": cfg})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]
