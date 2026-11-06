"""Connectors and sinks against real Postgres, MySQL and an S3-compatible server (docker compose).

    docker compose -f packages/label/docker/compose.yml -p caliper-label-test up -d --wait
    uv run pytest -m docker

Ports follow CALIPER_LABEL_PG_PORT / CALIPER_LABEL_MYSQL_PORT / CALIPER_LABEL_S3_PORT (defaults
55432 / 53306 / 59000), the same variables compose.yml uses. All data is fictional.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from caliper_label import sinks, snapshot
from caliper_label.app import create_app
from caliper_label.connectors import ConnectorError, open_connector
from caliper_label.connectors.duck import load_extension
from caliper_label.project import DisplayType, ProjectConfig, SourceConfig
from caliper_label.project_store import ProjectStore
from fastapi.testclient import TestClient

pytestmark = pytest.mark.docker

PG_PORT = int(os.environ.get("CALIPER_LABEL_PG_PORT", "55432"))
MY_PORT = int(os.environ.get("CALIPER_LABEL_MYSQL_PORT", "53306"))
S3_PORT = int(os.environ.get("CALIPER_LABEL_S3_PORT", "59000"))
S3_URL = f"http://127.0.0.1:{S3_PORT}"
BUCKET = "caliper-test"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)
S3_CREDS = {"endpoint_url": S3_URL, "region": "us-east-1"}


def _up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module", autouse=True)
def services() -> None:
    ports = (("postgres", PG_PORT), ("mysql", MY_PORT), ("s3", S3_PORT))
    deadline = time.monotonic() + 60  # the S3 container has no healthcheck: give it a moment
    while (down := [n for n, p in ports if not _up(p)]) and time.monotonic() < deadline:
        time.sleep(1)
    if down:
        pytest.fail(
            f"docker services not reachable: {', '.join(down)}. Start them with "
            "`docker compose -f packages/label/docker/compose.yml -p caliper-label-test up -d "
            "--wait`"
        )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k in list(os.environ):
        if k.startswith(("PG", "MYSQL_", "AWS_")) or k == "DATABASE_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", str(PG_PORT))
    monkeypatch.setenv("PGUSER", "caliper")
    monkeypatch.setenv("PGPASSWORD", "caliper-local-only")
    monkeypatch.setenv("PGDATABASE", "shop")
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_TCP_PORT", str(MY_PORT))
    monkeypatch.setenv("MYSQL_USER", "caliper")
    monkeypatch.setenv("MYSQL_PWD", "caliper-local-only")
    monkeypatch.setenv("MYSQL_DATABASE", "shop")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "caliper-s3-user")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "caliper-s3-local-only")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return monkeypatch


@pytest.fixture
def s3(env, tmp_path: Path):
    """Bucket with fictional csv/parquet parts and two images (uploaded through boto3)."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=S3_URL,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=BUCKET)
    except client.exceptions.ClientError as exc:
        if "BucketAlready" not in str(exc):
            raise
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT 'S3-' || lpad(i::VARCHAR, 3, '0') AS ticket_id, "
        "'Fictional parcel ' || i AS subject, ['low','high'][1 + i % 2] AS priority, "
        "'img/t' || (i % 2) || '.png' AS screenshot FROM range(1, 31) r(i)"
    )
    for k in range(3):
        p = tmp_path / f"part{k}.parquet"
        con.execute(f"COPY (SELECT * FROM t WHERE CAST(ticket_id[4:] AS INT) % 3 = {k}) TO '{p}'")
        client.upload_file(str(p), BUCKET, f"exports/part{k}.parquet")
    csvp = tmp_path / "t.csv"
    con.execute(f"COPY t TO '{csvp}' (HEADER)")
    client.upload_file(str(csvp), BUCKET, "exports/tickets.csv")
    for i in range(2):
        client.put_object(Bucket=BUCKET, Key=f"img/t{i}.png", Body=PNG, ContentType="image/png")
        client.put_object(Bucket=BUCKET, Key=f"exports/img/t{i}.png", Body=PNG)
    return client


def pg_exec(sql: str) -> None:
    con = duckdb.connect()
    load_extension(con, "postgres")
    con.execute(
        f"ATTACH 'host=127.0.0.1 port={PG_PORT} dbname=shop user=caliper' AS db (TYPE postgres)"
    )
    con.execute("CALL postgres_execute('db', ?)", [sql])
    con.close()


# --------------------------------------------------------------------------- postgres


def test_postgres_table(env, tmp_path: Path) -> None:
    c = open_connector(SourceConfig(kind="postgres", table="support.tickets"), tmp_path)
    assert c.count() == 40
    rows = c.preview(3)
    assert [r["ticket_id"] for r in rows] == ["PG-001", "PG-002", "PG-003"]
    assert json.loads(rows[0]["meta"]) == {"channel": "chat", "n": 1}  # jsonb arrives as text
    assert rows[0]["opened_at"].startswith("2026-10-01T10:00:00")
    cols = {col.name: col.display for col in c.schema()}
    assert cols["amount"] is DisplayType.NUMBER
    assert cols["meta"] is DisplayType.JSON
    assert cols["screenshot"] is DisplayType.IMAGE
    assert cols["body"] is DisplayType.LONG_TEXT
    assert cols["priority"] is DisplayType.BADGE
    assert sum(len(b) for b in c.iter_rows(batch_size=7)) == 40


def test_postgres_query_and_dsn_env(env, tmp_path: Path) -> None:
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        env.delenv(k)
    env.setenv("DATABASE_URL", f"postgresql://caliper:caliper-local-only@127.0.0.1:{PG_PORT}/shop")
    q = "select ticket_id, subject from support.tickets where priority = 'high' order by 1;"
    c = open_connector(SourceConfig(kind="postgres", query=q), tmp_path)
    assert c.count() == 13 and c.preview(1)[0]["ticket_id"] == "PG-002"
    env.delenv("DATABASE_URL")
    env.setenv("SHOP_DSN", f"postgresql://caliper:caliper-local-only@127.0.0.1:{PG_PORT}/shop")
    c = open_connector(
        SourceConfig(kind="postgres", query=q, credentials={"env": "SHOP_DSN"}), tmp_path
    )
    assert c.count() == 13


def test_postgres_bad_password_is_explained_and_redacted(env, tmp_path: Path) -> None:
    env.setenv("PGPASSWORD", "wrong-Fictional-pw-991")
    c = open_connector(SourceConfig(kind="postgres", table="support.tickets"), tmp_path)
    with pytest.raises(ConnectorError) as e:
        c.count()
    assert "wrong-Fictional-pw-991" not in e.value.message + (e.value.hint or "")
    assert e.value.hint and "password" in e.value.hint
    c = open_connector(SourceConfig(kind="postgres", table="support.nope"), tmp_path)
    env.setenv("PGPASSWORD", "caliper-local-only")
    with pytest.raises(ConnectorError) as e:
        c.count()
    assert "table" in (e.value.hint or "")


def test_postgres_snapshot_and_refresh(env, tmp_path: Path) -> None:
    pg_exec("DROP TABLE IF EXISTS support.queue_items")
    pg_exec(
        "CREATE TABLE support.queue_items AS SELECT ticket_id, subject, priority "
        "FROM support.tickets WHERE ticket_id <= 'PG-010'"
    )
    cfg = ProjectConfig.model_validate(
        {
            "source": {
                "kind": "postgres",
                "table": "support.queue_items",
                "id_column": "ticket_id",
            },
            "card": {"fields": [{"column": "subject"}]},
            "workflow": {"queues": {"by": "priority"}},
        }
    )
    ctx = SimpleNamespace(
        project_dir=tmp_path,
        config=cfg,
        store=ProjectStore.open(tmp_path),
        connector=open_connector(cfg.source, tmp_path),
    )
    assert snapshot.take(ctx).n_items == 10
    pg_exec("DELETE FROM support.queue_items WHERE ticket_id = 'PG-001'")
    pg_exec("UPDATE support.queue_items SET subject = 'Edited' WHERE ticket_id = 'PG-002'")
    pg_exec("INSERT INTO support.queue_items VALUES ('PG-900', 'Brand new', 'low')")
    d = snapshot.diff(ctx)
    assert (d.removed, d.changed, d.new) == (["PG-001"], ["PG-002"], ["PG-900"])
    snapshot.apply(ctx, d)
    assert ctx.store.item("PG-002").fields["subject"] == "Edited"
    assert ctx.store.item("PG-001").removed
    pg_exec("DROP TABLE support.queue_items")


def test_postgres_write_back(env, tmp_path: Path) -> None:
    pg_exec("DROP TABLE IF EXISTS support.export_labels")
    target = SourceConfig(kind="postgres", table="support.export_labels")
    rows = [{"item_id": f"PG-{i:03d}", "decision": "correct", "fields": {"n": i}} for i in range(3)]
    r = sinks.write(iter(rows), target, project_dir=tmp_path)
    assert r.created and r.rows == 3
    assert sinks.write(iter(rows[:1]), target, project_dir=tmp_path).created is False
    c = open_connector(target, tmp_path)
    assert c.count() == 4
    assert json.loads(c.preview(1)[0]["fields"]) == {"n": 0}
    with pytest.raises(ConnectorError):
        sinks.write(iter(rows), target, project_dir=tmp_path, if_exists="fail")
    sinks.write(iter(rows), target, project_dir=tmp_path, if_exists="replace")
    assert open_connector(target, tmp_path).count() == 3
    # append into the pre-existing seeded table, columns matched by name
    sinks.write(
        iter([{"decision": "wrong", "item_id": "PG-007"}]),
        SourceConfig(kind="postgres", table="support.review_labels"),
        project_dir=tmp_path,
    )
    pg_exec("DROP TABLE support.export_labels")


# --------------------------------------------------------------------------- mysql


def test_mysql_table_query_and_write_back(env, tmp_path: Path) -> None:
    c = open_connector(SourceConfig(kind="mysql", table="tickets"), tmp_path)
    assert c.count() == 40
    first = c.preview(1)[0]
    assert first["ticket_id"] == "MY-001" and first["amount"] == 3.25
    cols = {col.name: col.display for col in c.schema()}
    assert cols["amount"] is DisplayType.NUMBER and cols["priority"] is DisplayType.BADGE
    q = open_connector(
        SourceConfig(kind="mysql", query="SELECT * FROM tickets WHERE priority = 'high'"), tmp_path
    )
    assert q.count() == 13 and len(q.preview(50)) == 13
    target = SourceConfig(kind="mysql", table="export_labels")
    sinks.write(iter([{"item_id": "MY-001", "decision": "correct"}]), target, if_exists="replace")
    sinks.write(iter([{"item_id": "MY-002", "decision": "wrong"}]), target)
    assert open_connector(target, tmp_path).count() == 2


# --------------------------------------------------------------------------- S3


def test_s3_glob_csv_and_presigned_media(s3, tmp_path: Path) -> None:
    src = SourceConfig(kind="s3", path=f"s3://{BUCKET}/exports/part*.parquet", credentials=S3_CREDS)
    c = open_connector(src, tmp_path)
    assert c.count() == 30
    cols = {col.name: col.display for col in c.schema()}
    assert cols["screenshot"] is DisplayType.IMAGE
    csv_c = open_connector(
        SourceConfig(kind="s3", path=f"s3://{BUCKET}/exports/tickets.csv", credentials=S3_CREDS),
        tmp_path,
    )
    assert csv_c.count() == 30
    # relative key -> presigned URL under the source folder; it really downloads
    ref = c.resolve_media("img/t1.png")
    assert ref.kind == "url" and "X-Amz-Signature" in ref.url
    with urllib.request.urlopen(ref.url, timeout=10) as resp:
        assert resp.read() == PNG
    # the bucket is not public: the unsigned URL is refused
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{S3_URL}/{BUCKET}/exports/img/t1.png", timeout=10)


def test_s3_media_route_redirects_to_presigned(s3, tmp_path: Path, make_session) -> None:
    cfg = ProjectConfig.model_validate(
        {
            "source": {
                "kind": "s3",
                "path": f"s3://{BUCKET}/exports/tickets.csv",
                "id_column": "ticket_id",
                "credentials": S3_CREDS,
            },
            "card": {"fields": [{"column": "subject"}, {"column": "screenshot", "type": "image"}]},
        }
    )
    ctx = SimpleNamespace(
        project_dir=tmp_path,
        config=cfg,
        store=ProjectStore.open(tmp_path),
        connector=open_connector(cfg.source, tmp_path),
    )
    app = create_app(make_session())
    app.state.ctx = ctx
    client = TestClient(app)
    assert client.post("/api/source/snapshot").json()["n_items"] == 30
    r = client.get("/api/media/S3-001/screenshot", follow_redirects=False)
    assert r.status_code == 302 and "X-Amz-Signature" in r.headers["location"]
    with urllib.request.urlopen(r.headers["location"], timeout=10) as resp:
        assert resp.read() == PNG
    t = client.post("/api/source/test").json()
    assert t["ok"] and t["credentials"][0]["resolved"]


def test_s3_upload_sink_then_read_back(s3, tmp_path: Path) -> None:
    rows = [{"item_id": f"S3-{i:03d}", "decision": "correct", "fields": {"n": i}} for i in range(5)]
    for fmt in ("parquet", "jsonl", "csv"):
        url = f"s3://{BUCKET}/labels/run.{fmt}"
        t = SourceConfig(kind="s3", path=url, credentials=S3_CREDS)
        r = sinks.write(iter(rows), t, overwrite=True)
        assert r.rows == 5 and r.target == url
        assert open_connector(t, tmp_path).count() == 5
        with pytest.raises(ConnectorError) as e:
            sinks.write(iter(rows), t)
        assert "overwrite" in (e.value.hint or "")


def test_s3_wrong_secret_is_explained_and_redacted(s3, env, tmp_path: Path) -> None:
    env.setenv("AWS_SECRET_ACCESS_KEY", "wrong-Fictional-S3-secret-55")
    c = open_connector(
        SourceConfig(kind="s3", path=f"s3://{BUCKET}/exports/tickets.csv", credentials=S3_CREDS),
        tmp_path,
    )
    with pytest.raises(ConnectorError) as e:
        c.count()
    text = e.value.message + (e.value.hint or "")
    assert "wrong-Fictional-S3-secret-55" not in text
    assert e.value.hint
