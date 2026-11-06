"""Wizard + settings against a real Postgres (dev 1's compose, fictional data).

docker compose -f packages/label/docker/compose.yml up -d --wait postgres
uv run pytest -m docker packages/label/tests/test_setup_docker.py
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from caliper_label.app import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.docker
PG_PORT = int(os.environ.get("CALIPER_LABEL_PG_PORT", "55432"))
SECRET = "caliper-local-only"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    try:
        socket.create_connection(("127.0.0.1", PG_PORT), timeout=1).close()
    except OSError:
        pytest.skip(f"no Postgres on 127.0.0.1:{PG_PORT} (start packages/label/docker/compose.yml)")
    for k in list(os.environ):
        if k.startswith("PG") or k == "DATABASE_URL":
            monkeypatch.delenv(k)
    monkeypatch.setenv("APP_DB_URL", f"postgresql://caliper:{SECRET}@127.0.0.1:{PG_PORT}/shop")
    monkeypatch.setenv("CALIPER_LABEL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    app = create_app(ctx=None)
    with TestClient(app) as c:
        yield c
    if app.state.ctx is not None:
        app.state.ctx.store.close()


SOURCE = {"kind": "postgres", "table": "support.tickets", "id_column": "ticket_id",
          "credentials": {"env": "APP_DB_URL"}}  # fmt: skip


def test_postgres_wizard_label_settings_export(client: TestClient, tmp_path: Path) -> None:
    pv = client.post("/api/setup/preview", json={"source": SOURCE}).json()
    assert pv["ok"] is True, pv
    assert pv["count"] == 40
    assert any(c["name"] == "APP_DB_URL" and c["resolved"] for c in pv["credentials"])
    cols = {c["name"]: c["display"] for c in pv["columns"]}
    assert cols["meta"] == "json" and cols["amount"] == "number"
    assert SECRET not in str(pv)

    cfg = {
        "name": "PG triage",
        "source": SOURCE,
        "card": {
            "fields": [
                {"column": "subject"},
                {"column": "body", "type": "long_text"},
                {"column": "meta", "type": "json"},
            ]
        },
        "labels": {
            "options": [
                {"value": "ok", "hotkey": "1", "approved": True},
                {"value": "bad", "hotkey": "2"},
            ]
        },
        "workflow": {"queues": {"by": "priority"}},
    }
    r = client.post("/api/setup/create", json={"dir": "pg", "config": cfg})
    assert r.status_code == 200, r.text
    text = (tmp_path / "pg" / "caliper.label.yaml").read_text()
    assert "env: APP_DB_URL" in text and SECRET not in text

    for _ in range(20):
        item = client.get("/api/next", params={"labeller": "ana"}).json()["item"]
        lab = {"item_id": item["id"], "labeller": "ana", "decision": "ok"}
        assert client.post("/api/labels", json=lab).status_code == 200
    s = client.get("/api/settings").json()
    assert s["label_counts"] == {"ok": 20}
    s["config"]["labels"]["options"][0]["label"] = "Looks right"
    r = client.put("/api/settings", json={"config": s["config"], "revision": 1})
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert client.post("/api/source/refresh").json()["n_new"] == 0  # connector still works

    out = client.get("/api/export/download", params={"format": "csv"})
    assert out.status_code == 200, out.text
    assert out.text.count("\n") >= 21  # header + 20 labelled items
