"""Usability #3: images that fail say why (API detail + hint, server log, check routes).

The usability test's Postgres tickets hold ``s3://caliper-test/img/tN.png`` links. The project
source (Postgres) had no AWS settings, so signing fell back to the default AWS chain, found no
credentials and answered 502; the card only said "Image not available" and the console said
nothing. These tests pin the explanation, the log line and the check routes A's wizard calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from caliper_label.app import create_app
from caliper_label.connectors import media as media_mod
from caliper_label.connectors import open_connector
from caliper_label.project import ProjectConfig, SourceConfig
from caliper_label.project_store import ProjectStore
from caliper_label.routes_data import media_log
from fastapi.testclient import TestClient

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)


@pytest.fixture
def no_aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No AWS credentials anywhere (never the machine's real profile)."""
    import os

    for k in list(os.environ):
        if k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    media_log.seen.clear()


def _project(tmp_path: Path, make_session, rows: list[dict], **source) -> TestClient:
    proj = tmp_path / "proj"
    (proj / "data" / "img").mkdir(parents=True)
    (proj / "data" / "img" / "a.png").write_bytes(PNG)
    (proj / "data" / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg = ProjectConfig.model_validate(
        {
            "source": {"kind": "file", "path": "data/items.jsonl", "id_column": "id", **source},
            "card": {"fields": [{"column": "subject"}, {"column": "screenshot", "type": "image"}]},
        }
    )
    ctx = SimpleNamespace(
        project_dir=proj,
        config=cfg,
        store=ProjectStore.open(proj),
        connector=open_connector(cfg.source, proj),
    )
    app = create_app(make_session())
    app.state.ctx = ctx
    client = TestClient(app)
    assert client.post("/api/source/snapshot").status_code == 200
    return client


TICKETS = [
    {
        "id": f"PG-00{i}",
        "subject": f"Moonwick order {i}",
        "screenshot": f"s3://caliper-test/img/t{i % 2}.png",
    }
    for i in range(1, 4)
]


def test_s3_links_in_a_non_s3_source_explain_themselves(
    tmp_path: Path, make_session, no_aws, caplog: pytest.LogCaptureFixture
) -> None:
    client = _project(tmp_path, make_session, TICKETS)
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        for item in ("PG-001", "PG-002", "PG-003"):
            r = client.get(f"/api/media/{item}/screenshot", follow_redirects=False)
            assert r.status_code == 502
            body = r.json()
            assert body["detail"].startswith("can't sign s3:// image links: AWS credentials")
            assert "endpoint_url" in body["hint"] and "AWS_ACCESS_KEY_ID" in body["hint"]
    lines = [rec.getMessage() for rec in caplog.records if "not served" in rec.getMessage()]
    assert len(lines) == 1, lines  # one line per cause, not one per image
    assert "PG-001" in lines[0] and "Fix:" in lines[0] and "endpoint_url" in lines[0]

    # the card's JSON check says the same thing, always 200
    c = client.get("/api/media/PG-001/screenshot/check").json()
    assert c["ok"] is False and c["kind"] is None
    assert c["detail"].startswith("can't sign") and "endpoint_url" in c["hint"]
    assert c["value"] == "s3://caliper-test/img/t1.png"
    assert client.get("/api/media/PG-001/subject/check").status_code == 404  # not an image


def test_signed_link_is_probed_and_never_returned(
    tmp_path: Path, make_session, no_aws, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fictional-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional-secret-Zq9")
    client = _project(
        tmp_path, make_session, TICKETS, credentials={"endpoint_url": "http://127.0.0.1:9"}
    )
    seen: list[str] = []

    def fake(url: str, timeout: float = 4.0):
        seen.append(url)
        return answers.pop(0)

    monkeypatch.setattr(media_mod, "_probe", fake)
    answers = [(206, None), (404, None), (403, None), (400, None), (None, "Connection refused")]
    got = [client.get("/api/media/PG-001/screenshot/check").json() for _ in range(5)]
    assert got[0]["ok"] is True and got[0]["kind"] == "signed" and got[0]["detail"] is None
    assert "not in the bucket" in got[1]["detail"]
    assert "refused the signed link (403)" in got[2]["detail"]
    assert "endpoint" in got[3]["hint"] and "http://127.0.0.1:9" in got[3]["hint"]
    assert "could not reach 127.0.0.1:9" in got[4]["detail"]
    assert seen and all("X-Amz-Signature" in u for u in seen)
    for g in got:
        assert "X-Amz" not in json.dumps(g) and "fictional-secret" not in json.dumps(g)


def test_wizard_media_check_with_a_body_source(tmp_path: Path, make_session, no_aws) -> None:
    rows = [
        {"id": "a", "pic": "img/a.png"},
        {"id": "b", "pic": "img/missing.png"},
        {"id": "c", "pic": "https://e.invalid/c.png"},
        {"id": "d", "pic": None},
    ]
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "a.png").write_bytes(PNG)
    (tmp_path / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    client = TestClient(create_app(make_session()))  # no project open: the wizard's case
    src = {"kind": "file", "path": "rows.jsonl"}
    r = client.post(
        "/api/source/media-check",
        json={"source": src, "project_dir": str(tmp_path), "column": "pic", "n": 5},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is False and b["checked"] == 3 and b["failed"] == 1
    assert [x["ok"] for x in b["results"]] == [True, False, None]
    assert b["detail"] == "image not found" and b["hint"]
    assert b["results"][2]["kind"] == "url"  # web links are not fetched by the server

    # a column is required with a body source
    r = client.post("/api/source/media-check", json={"source": src, "project_dir": str(tmp_path)})
    assert r.status_code == 400 and r.json()["hint"]

    # s3:// links from a file source with no AWS settings: the reason and the fix
    (tmp_path / "s3.jsonl").write_text(json.dumps({"id": "x", "pic": "s3://b/k.png"}) + "\n")
    b = client.post(
        "/api/source/media-check",
        json={
            "source": {"kind": "file", "path": "s3.jsonl"},
            "project_dir": str(tmp_path),
            "column": "pic",
        },
    ).json()
    assert b["ok"] is False and "can't sign" in b["detail"] and "endpoint_url" in b["hint"]


def test_project_media_check_uses_the_image_fields(tmp_path: Path, make_session, no_aws) -> None:
    rows = [{"id": "x1", "subject": "Lamp", "screenshot": "img/a.png"}]
    client = _project(tmp_path, make_session, rows)
    b = client.post("/api/source/media-check").json()
    assert b["ok"] is True and b["checked"] == 1 and b["results"][0]["column"] == "screenshot"


def test_check_ref_never_fetches_plain_links() -> None:
    def boom(ref: str):
        raise AssertionError("must not resolve web links")

    out = media_mod.check_ref("https://e.invalid/p.png", boom, SourceConfig(kind="file"))
    assert out["ok"] is None and out["kind"] == "url"
    assert media_mod.check_ref("", boom)["ok"] is False
