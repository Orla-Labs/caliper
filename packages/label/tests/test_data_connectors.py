"""Connector interface, registry/plugins, file + folder connectors, type detection, media safety."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest
from caliper_label.connectors import (
    BaseConnector,
    Column,
    Connector,
    ConnectorError,
    available_connectors,
    open_connector,
    register,
    unregister,
)
from caliper_label.connectors.duck import load_extension, save_upload
from caliper_label.project import DisplayType, SourceConfig
from caliper_label.typedetect import detect_display

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)

ROWS = [
    {
        "ticket_id": f"T-{i:03d}",
        "subject": f"Moonwick order {i} arrived late",
        "body": "Customer writes:\n" + "the parcel was late. " * (i % 3 + 1),
        "priority": ["high", "low", "medium"][i % 3],
        "score": round(0.1 * i, 2),
        "url": f"https://example.invalid/tickets/{i}",
        "photo": f"img/p{i % 2}.png",
        "meta": {"channel": "email", "n": i},
    }
    for i in range(12)
]


def has_ext(name: str) -> bool:
    try:
        load_extension(duckdb.connect(), name)
        return True
    except ConnectorError:
        return False


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    (d / "data" / "img").mkdir(parents=True)
    for i in range(2):
        (d / "data" / "img" / f"p{i}.png").write_bytes(PNG)
    jsonl = d / "data" / "tickets.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t AS SELECT * FROM read_json('{jsonl}')")
    con.execute(f"COPY (SELECT * EXCLUDE (meta) FROM t) TO '{d / 'data' / 'tickets.csv'}'")
    con.execute(
        f"COPY (SELECT * EXCLUDE (meta) FROM t) TO '{d / 'data' / 'tickets.tsv'}' (DELIM '\t')"
    )
    con.execute(f"COPY t TO '{d / 'data' / 'tickets.parquet'}' (FORMAT parquet)")
    (d / "data" / "tickets.json").write_text(json.dumps(ROWS))
    (d / "data" / "part").mkdir()
    for k in range(3):
        out = d / "data" / "part" / f"p{k}.parquet"
        con.execute(
            f"COPY (SELECT * FROM t WHERE CAST(ticket_id[3:] AS INT) % 3 = {k}) TO '{out}' "
            "(FORMAT parquet)"
        )
    return d


def src(**kw: Any) -> SourceConfig:
    return SourceConfig.model_validate(kw)


# --------------------------------------------------------------------------- files


@pytest.mark.parametrize(
    "path",
    [
        "data/tickets.csv",
        "data/tickets.tsv",
        "data/tickets.jsonl",
        "data/tickets.json",
        "data/tickets.parquet",
    ],
)
def test_file_formats_schema_preview_count(proj: Path, path: str) -> None:
    c = open_connector(src(kind="file", path=path), proj)
    assert isinstance(c, Connector)
    assert c.count() == 12
    rows = c.preview(5)
    assert len(rows) == 5 and rows[0]["ticket_id"] == "T-000"
    cols = {col.name: col for col in c.schema()}
    assert cols["score"].display is DisplayType.NUMBER
    assert cols["url"].display is DisplayType.LINK
    assert cols["photo"].display is DisplayType.IMAGE
    assert cols["priority"].display is DisplayType.BADGE
    assert cols["body"].display is DisplayType.LONG_TEXT
    assert cols["subject"].display is DisplayType.TEXT
    if "meta" in cols:
        assert cols["meta"].display is DisplayType.JSON
    batches = list(c.iter_rows(batch_size=5))
    assert [len(b) for b in batches] == [5, 5, 2]
    json.dumps(batches)  # JSON-safe


def test_glob_reads_all_parts(proj: Path) -> None:
    c = open_connector(src(kind="file", path="data/part/*.parquet"), proj)
    assert c.count() == 12
    ids = sorted(r["ticket_id"] for b in c.iter_rows() for r in b)
    assert ids == sorted(r["ticket_id"] for r in ROWS)


def test_absolute_path_and_explicit_format(proj: Path, tmp_path: Path) -> None:
    odd = tmp_path / "export.txt"
    odd.write_text((proj / "data" / "tickets.csv").read_text())
    with pytest.raises(ConnectorError) as e:
        open_connector(src(kind="file", path=str(odd)), proj)
    assert "source.format" in (e.value.hint or "")
    c = open_connector(src(kind="file", path=str(odd), format="csv"), proj)
    assert c.count() == 12


def test_missing_file_has_hint(proj: Path) -> None:
    c = open_connector(src(kind="file", path="data/nope.csv"), proj)
    with pytest.raises(ConnectorError) as e:
        c.count()
    assert "not found" in e.value.message and "project folder" in (e.value.hint or "")


@pytest.mark.skipif(not has_ext("excel"), reason="DuckDB excel extension unavailable (offline)")
def test_xlsx(proj: Path) -> None:
    con = duckdb.connect()
    load_extension(con, "excel")
    out = proj / "data" / "tickets.xlsx"
    con.execute(
        f"COPY (SELECT ticket_id, subject, priority, score FROM read_csv('"
        f"{proj / 'data' / 'tickets.csv'}')) TO '{out}' (FORMAT xlsx, HEADER true)"
    )
    c = open_connector(src(kind="file", path="data/tickets.xlsx"), proj)
    assert c.count() == 12
    assert c.preview(1)[0]["ticket_id"] == "T-000"


def test_save_upload_sanitises_and_dedupes(tmp_path: Path) -> None:
    import io

    p1 = save_upload(tmp_path / "data", "../../evil name.csv", io.BytesIO(b"a\n1\n"))
    p2 = save_upload(tmp_path / "data", "evil name.csv", io.BytesIO(b"a\n1\n"))
    assert p1.parent == tmp_path / "data" and p1.name == "evil_name.csv"
    assert p2.name == "evil_name-1.csv"
    with pytest.raises(ConnectorError):
        save_upload(tmp_path / "data", "x.exe", io.BytesIO(b""))


# --------------------------------------------------------------------------- folder


def test_folder_of_images_with_sidecar(proj: Path) -> None:
    folder = proj / "photos"
    (folder / "sub").mkdir(parents=True)
    for n in ("b.png", "a.jpg", "sub/c.webp"):
        (folder / n).write_bytes(PNG)
    (folder / "notes.txt").write_text("not an image")
    (proj / "photos.csv").write_text("file_name,caption,label\na.jpg,A cat,cat\nc.webp,A dog,dog\n")
    c = open_connector(src(kind="folder", path="photos", sidecar="photos.csv"), proj)
    rows = c.preview()
    assert [r["image"] for r in rows] == ["a.jpg", "b.png", "sub/c.webp"]
    assert rows[0]["caption"] == "A cat" and rows[1]["caption"] is None
    assert rows[2]["label"] == "dog"
    assert c.count() == 3
    cols = {col.name: col.display for col in c.schema()}
    assert cols["image"] is DisplayType.IMAGE
    ref = c.resolve_media("sub/c.webp")
    assert ref.kind == "file" and ref.path.endswith("c.webp") and ref.mime == "image/webp"
    without = open_connector(src(kind="folder", path="photos"), proj)
    assert set(without.preview()[0]) == {"image", "file_name"}


def test_folder_missing(proj: Path) -> None:
    with pytest.raises(ConnectorError):
        open_connector(src(kind="folder", path="nope"), proj)


# --------------------------------------------------------------------------- media safety


def test_media_local_paths_are_confined(proj: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.png").write_bytes(PNG)
    (proj / "data" / "img" / "notes.txt").write_text("x")
    c = open_connector(src(kind="file", path="data/tickets.csv"), proj)
    ok = c.resolve_media("img/p0.png")  # relative to the data folder
    assert ok.kind == "file" and Path(ok.path).read_bytes() == PNG
    assert c.resolve_media("data/img/p1.png").kind == "file"  # relative to the project
    for bad in ("../../secret.png", str(tmp_path / "secret.png"), "img/../../../secret.png"):
        with pytest.raises(ConnectorError) as e:
            c.resolve_media(bad)
        assert "outside" in e.value.message
    with pytest.raises(ConnectorError):
        c.resolve_media("img/notes.txt")  # not an image type
    with pytest.raises(ConnectorError):
        c.resolve_media("img/p0.png\x00.txt")
    link = proj / "data" / "img" / "escape.png"
    os.symlink(tmp_path / "secret.png", link)
    with pytest.raises(ConnectorError):
        c.resolve_media("img/escape.png")  # symlink leaving the roots


def test_media_extra_roots_and_urls(proj: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "s.png").write_bytes(PNG)
    c = open_connector(
        src(kind="file", path="data/tickets.csv", options={"media_roots": [str(shared)]}), proj
    )
    assert c.resolve_media(str(shared / "s.png")).kind == "file"
    u = c.resolve_media("https://example.invalid/a.png?x=1")
    assert u.kind == "url" and u.url.startswith("https://") and u.mime == "image/png"


def test_media_s3_is_presigned(proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFICTIONAL0000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional-secret-value-for-tests")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    c = open_connector(
        src(
            kind="s3",
            path="s3://moonwick-data/exports/tickets.parquet",
            credentials={"region": "eu-west-1", "endpoint_url": "http://localhost:59000"},
        ),
        proj,
    )
    ref = c.resolve_media("s3://moonwick-data/img/a.png")
    assert ref.kind == "url" and "X-Amz-Signature" in ref.url and "X-Amz-Expires=300" in ref.url
    assert ref.url.startswith("http://localhost:59000/moonwick-data/img/a.png")
    assert "fictional-secret-value" not in ref.url
    rel = c.resolve_media("img/b.png")  # relative to the source's folder
    assert "/moonwick-data/exports/img/b.png" in rel.url


# --------------------------------------------------------------------------- type detection


@pytest.mark.parametrize(
    ("dtype", "values", "expected"),
    [
        ("BIGINT", [1, 2], DisplayType.NUMBER),
        ("DECIMAL(10,2)", [1.5], DisplayType.NUMBER),
        ("BOOLEAN", [True], DisplayType.BADGE),
        ("STRUCT(a INTEGER)", [{"a": 1}], DisplayType.JSON),
        ("VARCHAR[]", [["a"]], DisplayType.JSON),
        ("JSON", ['{"a":1}'], DisplayType.JSON),
        ("VARCHAR", ['{"a": 1}', "[1, 2]"], DisplayType.JSON),
        (
            "VARCHAR",
            ["https://x.invalid/a.JPG", "https://x.invalid/b.png?sig=1"],
            DisplayType.IMAGE,
        ),
        ("VARCHAR", ["s3://bucket/a.png", "s3://bucket/b.webp"], DisplayType.IMAGE),
        ("VARCHAR", ["imgs/a.png", "/abs/b.jpeg"], DisplayType.IMAGE),
        ("VARCHAR", ["https://x.invalid/a", "http://x.invalid/b"], DisplayType.LINK),
        ("VARCHAR", ["red", "blue", "red", "blue", "red"], DisplayType.BADGE),
        ("VARCHAR", ["a" * 200], DisplayType.LONG_TEXT),
        ("VARCHAR", ["line\nbreak"], DisplayType.LONG_TEXT),
        ("VARCHAR", ["one", "two", "three"], DisplayType.TEXT),
        ("VARCHAR", [None, ""], DisplayType.TEXT),
        ("unknown", [1, 2.5], DisplayType.NUMBER),
        ("unknown", [{"x": 1}], DisplayType.JSON),
    ],
)
def test_detect_display(dtype: str, values: list[Any], expected: DisplayType) -> None:
    assert detect_display(dtype, values) is expected


# --------------------------------------------------------------------------- custom connectors


class ListConnector(BaseConnector):
    """A custom connector in a few lines: only iter_rows is required."""

    def iter_rows(self, batch_size: int = 1000):
        rows = [
            {"id": i, "text": f"fictional row {i}"} for i in range(int(self.source.options["n"]))
        ]
        for i in range(0, len(rows), batch_size):
            yield rows[i : i + batch_size]


def test_custom_connector_registered_in_process(tmp_path: Path) -> None:
    register("listy", ListConnector)
    try:
        c = open_connector(src(kind="plugin", plugin="listy", options={"n": 7}), tmp_path)
        assert c.count() == 7
        assert c.preview(2) == [
            {"id": 0, "text": "fictional row 0"},
            {"id": 1, "text": "fictional row 1"},
        ]
        assert [col.name for col in c.schema()] == ["id", "text"]
        assert isinstance(c.schema()[0], Column) and c.schema()[0].display is DisplayType.NUMBER
        assert available_connectors()["listy"] == "registered"
    finally:
        unregister("listy")


def test_plugin_discovered_via_entry_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real installed-distribution entry point (a dist-info on sys.path), not a mock."""
    site = tmp_path / "site"
    pkg = site / "moonwick_connector"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            """
            from caliper_label.connectors import BaseConnector

            class Warehouse(BaseConnector):
                def iter_rows(self, batch_size=1000):
                    yield [{"sku": "MW-1", "name": "Moonwick lamp"},
                           {"sku": "MW-2", "name": "Wick"}]
            """
        )
    )
    di = site / "moonwick_connector-0.1.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: moonwick-connector\nVersion: 0.1\n")
    (di / "entry_points.txt").write_text(
        "[caliper_label.connectors]\nwarehouse = moonwick_connector:Warehouse\n"
    )
    monkeypatch.syspath_prepend(str(site))
    assert available_connectors()["warehouse"] == "plugin:moonwick-connector"
    c = open_connector(src(kind="plugin", plugin="warehouse"), tmp_path)
    assert c.count() == 2 and c.preview(1) == [{"sku": "MW-1", "name": "Moonwick lamp"}]
    sys.modules.pop("moonwick_connector", None)


def test_unknown_plugin_and_bad_plugin(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError) as e:
        open_connector(src(kind="plugin", plugin="nope"), tmp_path)
    assert e.value.hint and "install" in e.value.hint
    register("broken", lambda s, p: object())
    try:
        with pytest.raises(ConnectorError) as e:
            open_connector(src(kind="plugin", plugin="broken"), tmp_path)
        assert "BaseConnector" in (e.value.hint or "")
    finally:
        unregister("broken")


# --------------------------------------------------------------------------- extensions offline


def test_extension_offline_gives_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIPER_LABEL_DUCKDB_EXTENSION_DIR", str(tmp_path / "empty-ext"))
    monkeypatch.setenv("CALIPER_LABEL_DUCKDB_EXTENSION_REPO", "http://127.0.0.1:9")
    db = tmp_path / "x.sqlite"
    import sqlite3

    sqlite3.connect(db).execute("CREATE TABLE t (a)").connection.commit()
    c = open_connector(src(kind="sqlite", path=str(db), table="t"), tmp_path)
    with pytest.raises(ConnectorError) as e:
        c.count()
    assert "extension 'sqlite'" in e.value.message and "could not be downloaded" in e.value.message
    assert (
        "internet" in (e.value.hint or "") and "CALIPER_LABEL_DUCKDB_EXTENSION_DIR" in e.value.hint
    )


# --------------------------------------------------------------------------- sqlite + http


@pytest.mark.skipif(not has_ext("sqlite"), reason="DuckDB sqlite extension unavailable (offline)")
def test_sqlite_table_and_query(tmp_path: Path) -> None:
    import sqlite3

    db = sqlite3.connect(tmp_path / "shop.db")
    db.execute("CREATE TABLE tickets (id TEXT, subject TEXT, priority TEXT)")
    db.executemany(
        "INSERT INTO tickets VALUES (?, ?, ?)",
        [(f"S-{i}", f"Fictional {i}", "high" if i % 2 else "low") for i in range(9)],
    )
    db.commit()
    db.close()
    t = open_connector(src(kind="sqlite", path="shop.db", table="tickets"), tmp_path)
    assert t.count() == 9 and t.preview(1) == [
        {"id": "S-0", "subject": "Fictional 0", "priority": "low"}
    ]
    q = open_connector(
        src(kind="sqlite", path="shop.db", query="SELECT id FROM tickets WHERE priority = 'high';"),
        tmp_path,
    )
    assert q.count() == 4
    with pytest.raises(ConnectorError):
        open_connector(src(kind="sqlite", path="shop.db"), tmp_path)  # neither table nor query
    with pytest.raises(ConnectorError):
        open_connector(src(kind="sqlite", path="nope.db", table="t"), tmp_path)


@pytest.mark.skipif(not has_ext("httpfs"), reason="DuckDB httpfs extension unavailable (offline)")
def test_http_source_with_bearer_token(proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.server
    import threading

    data = (proj / "data" / "tickets.csv").read_bytes()
    seen: list[str | None] = []

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bool) -> None:
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Bearer fictional-token-123":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_HEAD(self) -> None:
            self._send(False)

        def do_GET(self) -> None:
            self._send(True)

        def log_message(self, *a: Any) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/tickets.csv"
    try:
        monkeypatch.setenv("TICKETS_TOKEN", "fictional-token-123")
        c = open_connector(src(kind="http", path=url, credentials={"env": "TICKETS_TOKEN"}), proj)
        assert c.count() == 12
        assert "Bearer fictional-token-123" in seen
        anon = open_connector(src(kind="http", path=url), proj)
        with pytest.raises(ConnectorError) as e:
            anon.count()
        assert e.value.hint and "fictional-token-123" not in e.value.message
        monkeypatch.delenv("TICKETS_TOKEN")
        with pytest.raises(ConnectorError) as e:
            open_connector(
                src(kind="http", path=url, credentials={"env": "TICKETS_TOKEN"}), proj
            ).count()
        assert "TICKETS_TOKEN" in e.value.message
    finally:
        srv.shutdown()
