"""routes_data through the real app, credential status, and the secrets-never-leak guarantee."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from caliper_label.app import create_app
from caliper_label.connectors import open_connector
from caliper_label.credentials import credential_status, db_params, redact
from caliper_label.project import ProjectConfig, SourceConfig
from caliper_label.project_store import ProjectStore
from fastapi.testclient import TestClient

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)
SENTINEL = "Zq9-fictional-S3cr3t-7731"


@pytest.fixture
def project(tmp_path: Path, make_session) -> tuple[TestClient, SimpleNamespace]:
    proj = tmp_path / "proj"
    (proj / "data" / "img").mkdir(parents=True)
    (proj / "data" / "img" / "a.png").write_bytes(PNG)
    (tmp_path / "outside.png").write_bytes(PNG)
    rows = [
        {
            "id": "x1",
            "title": "Moonwick lamp",
            "pic": "img/a.png",
            "remote": "https://e.invalid/1.png",
        },
        {"id": "x2", "title": "Wick holder", "pic": "../../outside.png", "remote": None},
        {"id": "x3", "title": "Glass cover", "pic": "img/missing.png", "remote": None},
    ]
    (proj / "data" / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg = ProjectConfig.model_validate(
        {
            "source": {"kind": "file", "path": "data/items.jsonl", "id_column": "id"},
            "card": {
                "fields": [
                    {"column": "title"},
                    {"column": "pic", "type": "image"},
                    {"column": "remote", "type": "image"},
                ]
            },
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
    return TestClient(app), ctx


def test_preview_with_body_source_before_a_project(tmp_path: Path, make_session) -> None:
    (tmp_path / "t.csv").write_text("a,b\n1,x\n2,y\n")
    client = TestClient(create_app(make_session()))  # legacy app, no ctx
    r = client.post(
        "/api/source/preview",
        json={"source": {"kind": "file", "path": "t.csv"}, "project_dir": str(tmp_path), "n": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2 and body["rows"] == [{"a": 1, "b": "x"}]
    assert [c["display"] for c in body["columns"]] == ["number", "text"]
    assert body["credentials"][0]["resolved"] is True
    # project routes need a project
    r = client.post("/api/source/refresh")
    assert r.status_code == 409 and r.json() == {
        "detail": "no project open",
        "hint": "open or create a project first",
    }
    assert client.get("/api/source/connectors").json()["connectors"]["postgres"] == "builtin"


def test_test_connection_reports_errors_with_hint(tmp_path: Path, make_session) -> None:
    client = TestClient(create_app(make_session()))
    r = client.post(
        "/api/source/test",
        json={"source": {"kind": "file", "path": "nope.csv"}, "project_dir": str(tmp_path)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "not found" in body["detail"] and body["hint"]
    r = client.post("/api/source/preview", json={"source": {"kind": "file", "path": "x.weird"}})
    assert r.status_code == 400 and set(r.json()) == {"detail", "hint"}


def test_project_preview_snapshot_refresh(project) -> None:
    client, ctx = project
    assert client.get("/api/source/preview?n=2").json()["count"] == 3
    t = client.post("/api/source/test").json()
    assert t["ok"] and t["count"] == 3
    snap = client.post("/api/source/snapshot").json()
    assert snap["n_items"] == 3 and snap["kind"] == "take"
    path = ctx.project_dir / "data" / "items.jsonl"
    path.write_text(path.read_text() + json.dumps({"id": "x4", "title": "New"}) + "\n")
    d = client.post("/api/source/refresh").json()
    assert d["n_new"] == 1 and d["new"] == ["x4"]
    ids = client.get(f"/api/source/refresh/{d['diff_id']}/ids?which=new").json()
    assert ids["ids"] == ["x4"]
    a = client.post("/api/source/refresh/apply", json={"diff_id": d["diff_id"]}).json()
    assert a["n_new"] == 1 and a["n_items"] == 4
    again = client.post("/api/source/refresh/apply", json={"diff_id": d["diff_id"]})
    assert again.status_code == 400 and "Refresh" in again.json()["hint"]
    hist = client.get("/api/source/snapshots").json()
    assert [s["kind"] for s in hist["snapshots"]] == ["refresh", "take"] and hist["items"] == 4


def test_media_endpoint(project) -> None:
    client, _ = project
    client.post("/api/source/snapshot")
    r = client.get("/api/media/x1/pic")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    assert client.get("/api/media/x1/1").content == PNG  # by card field index
    r = client.get("/api/media/x1/remote", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://e.invalid/1.png"
    r = client.get("/api/media/x2/pic")
    assert r.status_code == 403 and "outside" in r.json()["detail"]
    assert client.get("/api/media/x3/pic").status_code == 404
    assert client.get("/api/media/x2/remote").status_code == 404  # empty value
    assert client.get("/api/media/x1/title").status_code == 404  # not an image field
    assert client.get("/api/media/nope/pic").status_code == 404
    assert client.get("/api/media/x1/..%2F..%2Fsecret").status_code == 404


# --------------------------------------------------------------------------- credentials


def test_credential_status_names_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("DATABASE_URL", "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPORT"):
        monkeypatch.delenv(v, raising=False)
    pg = SourceConfig(kind="postgres", table="tickets")
    st = credential_status(pg)
    assert st[0].resolved is False and "DATABASE_URL" in st[0].hint
    monkeypatch.setenv("PGHOST", "db.fictional.invalid")
    monkeypatch.setenv("PGPASSWORD", SENTINEL)
    st = credential_status(pg)
    assert st[0].resolved and st[0].name == "PGHOST, PGPASSWORD"
    assert SENTINEL not in json.dumps([c.model_dump() for c in st])

    mapped = SourceConfig(
        kind="postgres", table="t", credentials={"env_map": {"password": "APP_DB_PW"}}
    )
    st = credential_status(mapped)
    assert st[0].name == "APP_DB_PW" and not st[0].resolved and "export APP_DB_PW" in st[0].hint

    monkeypatch.setenv(
        "APP_DSN", f"postgresql://ana:{SENTINEL}@db.invalid:5433/shop?sslmode=require"
    )
    dsn = SourceConfig(kind="postgres", table="t", credentials={"env": "APP_DSN"})
    p = db_params(dsn)
    assert (p.host, p.port, p.user, p.database) == ("db.invalid", "5433", "ana", "shop")
    assert ("sslmode", "require") in p.extra and SENTINEL not in repr(p)
    assert credential_status(dsn)[0].name == "APP_DSN"


def test_aws_status_and_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    s3 = SourceConfig(kind="s3", path="s3://b/k.csv")
    st = credential_status(s3)
    assert not st[0].resolved and "AWS_PROFILE" in st[0].hint and "aws sso login" in st[0].hint
    prof = SourceConfig(kind="s3", path="s3://b/k.csv", credentials={"aws_profile": "dev"})
    assert "--profile dev" in credential_status(prof)[0].hint
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFICTIONAL0000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL)
    st = credential_status(s3)
    assert st[0].resolved and st[0].name.startswith("AWS chain")
    assert SENTINEL not in json.dumps([c.model_dump() for c in st])


def test_redact() -> None:
    assert "hunter2x" not in redact("postgresql://u:hunter2x@h/db failed")
    assert "hunter2x" not in redact("password=hunter2x host=h")


def test_secrets_never_leak(
    tmp_path: Path, make_session, monkeypatch: pytest.MonkeyPatch, caplog, capsys
) -> None:
    """Failing and succeeding calls: the secret is in no response, log line, file or DB."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "1")  # nothing listens: connection refused
    monkeypatch.setenv("PGUSER", "caliper")
    monkeypatch.setenv("PGPASSWORD", SENTINEL)
    monkeypatch.setenv("APP_DSN", f"postgresql://caliper:{SENTINEL}@127.0.0.1:1/shop")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFICTIONAL0000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL)
    monkeypatch.setenv("TOKEN_VAR", SENTINEL)
    (tmp_path / "t.csv").write_text("a\n1\n")
    app = create_app(make_session())
    client = TestClient(app)
    sources = [
        {"kind": "postgres", "table": "tickets"},
        {"kind": "postgres", "query": "select 1", "credentials": {"env": "APP_DSN"}},
        {"kind": "mysql", "table": "t", "credentials": {"env": "APP_DSN"}},
        {
            "kind": "s3",
            "path": "s3://fictional-bucket/x.csv",
            "credentials": {"endpoint_url": "http://127.0.0.1:1"},
        },
        {"kind": "http", "path": "http://127.0.0.1:1/x.csv", "credentials": {"env": "TOKEN_VAR"}},
        {"kind": "file", "path": "t.csv"},
    ]
    bodies = []
    for s in sources:
        for route in ("test", "preview", "credentials"):
            r = client.post(
                f"/api/source/{route}", json={"source": s, "project_dir": str(tmp_path)}
            )
            bodies.append(r.text)
    assert any('"ok":false' in b for b in bodies)  # the failures really happened
    out = capsys.readouterr()
    everything = "\n".join([*bodies, caplog.text, out.out, out.err])
    assert SENTINEL not in everything
    # the project store and config hold references only
    proj = tmp_path / "p"
    store = ProjectStore.open(proj)
    store.set_meta("x", "y")
    store.close()
    cfg = ProjectConfig(source=SourceConfig.model_validate(sources[1]))
    assert SENTINEL not in cfg.model_dump_json()
    for f in proj.rglob("*"):
        if f.is_file():
            assert SENTINEL.encode() not in f.read_bytes()


def test_redact_keeps_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PWD", str(tmp_path))
    monkeypatch.setenv("OLDPWD", str(tmp_path))
    monkeypatch.setenv("MYSQL_PWD", SENTINEL)
    monkeypatch.setenv("KEYBOARD_LAYOUT", "us-international")  # not a secret: no word match
    text = f"{tmp_path}/data/x.csv not found; us-international; {SENTINEL}"
    out = redact(text)
    assert f"{tmp_path}/data/x.csv" in out and "us-international" in out
    assert SENTINEL not in out


def test_gcs_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    for v in ("GCS_HMAC_KEY_ID", "GCS_HMAC_SECRET", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "no-gcloud"))
    real_import = builtins.__import__

    def no_gcsfs(name, *a, **kw):
        if name == "gcsfs":
            raise ImportError("no gcsfs")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_gcsfs)
    gcs = SourceConfig(kind="gcs", path="gs://fictional-bucket/batch.jsonl")
    st = credential_status(gcs)
    assert not st[0].resolved and "gcloud auth application-default login" in st[0].hint
    with pytest.raises(Exception) as e:
        open_connector(gcs, tmp_path).count()
    assert "GCS credentials not found" in str(e.value)
    assert "GCS_HMAC_KEY_ID" in e.value.hint and "gcloud" in e.value.hint
    monkeypatch.setenv("GCS_HMAC_KEY_ID", "GOOGFICTIONAL0000")
    monkeypatch.setenv("GCS_HMAC_SECRET", SENTINEL)
    st = credential_status(gcs)
    assert st[0].resolved and st[0].name == "GCS_HMAC_KEY_ID, GCS_HMAC_SECRET"
    conn = open_connector(gcs, tmp_path)
    try:
        conn.con()  # setup creates the in-memory secret; no network needed
    except Exception as exc:  # only acceptable failure: httpfs not downloadable offline
        assert "extension 'httpfs'" in str(exc)
    else:
        rows = conn.con().execute("SELECT name, type FROM duckdb_secrets()").fetchall()
        assert ("caliper_gcs", "gcs") in rows
        assert SENTINEL not in str(conn.con().execute("FROM duckdb_secrets()").fetchall())
