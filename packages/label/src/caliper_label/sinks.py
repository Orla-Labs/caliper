"""Export sinks (dev 3 calls these): write an iterator of row dicts somewhere.

    sinks.write(rows, SourceConfig(kind="file", path="out/labels.parquet"), project_dir=p)
    sinks.write(rows, SourceConfig(kind="s3", path="s3://bucket/labels/run.jsonl"))
    sinks.write(rows, SourceConfig(kind="postgres", table="review.labels"), if_exists="append")

* Files: ``csv``, ``jsonl``, ``parquet`` (format from ``fmt``, ``target.format`` or the extension).
  An existing file is only replaced with ``overwrite=True``. Writes go to a temp file first, then
  an atomic rename.
* S3 (and S3-compatible endpoints): the file is built locally, then uploaded with the source's
  credentials (same chain as reading).
* Databases (postgres/mysql/sqlite): ``target.table`` is created if missing, else appended to
  (columns matched by name). ``if_exists="fail"`` refuses an existing table. A table is only ever
  dropped with ``if_exists="replace"``: the explicit flag.

Rows stream through a temporary JSONL file, so memory stays flat. For csv, parquet and tables,
nested values (dicts/lists) are stored as JSON text.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import credentials as creds
from .connectors.base import ConnectorError, jsonable
from .connectors.duck import (
    MySQLConnector,
    PostgresConnector,
    SQLiteConnector,
    _new_connection,
    lit,
    qualified,
    translate,
)
from .project import SourceConfig, SourceKind

FILE_FORMATS = ("csv", "jsonl", "parquet")
_EXT = {".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl", ".parquet": "parquet"}


class SinkResult(BaseModel):
    target: str  # path, s3 URL or table (never credentials)
    format: str
    rows: int
    created: bool | None = None  # tables: True if this write created it


def _fmt(path: str | None, fmt: str | None, target: SourceConfig) -> str:
    f = (fmt or target.format or "").lower().lstrip(".")
    if not f and path:
        f = _EXT.get(Path(path.split("?", 1)[0]).suffix.lower(), "")
    if f not in FILE_FORMATS:
        raise ConnectorError(
            f"can't write format '{f or '?'}'",
            f"use one of: {', '.join(FILE_FORMATS)} (or a .csv/.jsonl/.parquet path)",
        )
    return f


def _flat(v: Any) -> Any:
    v = jsonable(v)
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def _spool(rows: Iterable[dict[str, Any]], path: Path, flatten: bool) -> tuple[int, list[str]]:
    """Write rows as JSONL; returns (count, column order of first appearance)."""
    n = 0
    cols: dict[str, None] = {}
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            out = {str(k): (_flat(v) if flatten else jsonable(v)) for k, v in row.items()}
            cols.update(dict.fromkeys(out))
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
            n += 1
    return n, list(cols)


def _reader(spool: Path, cols: list[str]) -> str:
    # every column read as text-or-native by inference over all rows; key order kept
    return (
        f"(SELECT {', '.join(_q(c) for c in cols)} FROM read_json({lit(str(spool))}, "
        "format = 'newline_delimited', sample_size = -1, union_by_name = true))"
    )


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _write_file(rows: Iterable[dict[str, Any]], dest: Path, fmt: str, overwrite: bool) -> int:
    if dest.exists() and not overwrite:
        raise ConnectorError(f"{dest.name} already exists", "pass overwrite=True to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".caliper-export-") as td:
        tmp_out = Path(td) / ("out." + fmt)
        if fmt == "jsonl":
            n, _ = _spool(rows, tmp_out, flatten=False)
        else:
            spool = Path(td) / "rows.jsonl"
            n, cols = _spool(rows, spool, flatten=True)
            con = _new_connection()
            try:
                if n == 0:
                    if fmt == "csv":
                        tmp_out.write_text("")
                    else:
                        con.execute(
                            f"COPY (SELECT NULL::VARCHAR AS empty WHERE false) TO "
                            f"{lit(str(tmp_out))} (FORMAT parquet)"
                        )
                else:
                    opts = "FORMAT csv, HEADER true" if fmt == "csv" else "FORMAT parquet"
                    con.execute(f"COPY {_reader(spool, cols)} TO {lit(str(tmp_out))} ({opts})")
            except Exception as exc:
                raise translate(exc, None, "export failed") from None
            finally:
                con.close()
        os.replace(tmp_out, dest)
    return n


def _upload_s3(
    rows: Iterable[dict[str, Any]], target: SourceConfig, fmt: str, overwrite: bool
) -> int:
    import boto3
    from botocore.config import Config

    url = target.path or ""
    if not url.startswith("s3://"):
        raise ConnectorError(f"not an S3 URL: {url}", "use s3://bucket/path/labels.parquet")
    bucket, _, key = url[5:].partition("/")
    if not bucket or not key or key.endswith("/"):
        raise ConnectorError(f"no object key in {url}", "use s3://bucket/path/labels.parquet")
    c = creds.aws_credentials(target)
    ep = target.credentials.endpoint_url
    client = boto3.client(
        "s3",
        aws_access_key_id=c.key_id,
        aws_secret_access_key=c.secret,
        aws_session_token=c.token,
        region_name=c.region or "us-east-1",
        endpoint_url=ep or None,
        config=Config(s3={"addressing_style": "path" if ep else "auto"}),
    )
    try:
        if not overwrite:
            try:
                client.head_object(Bucket=bucket, Key=key)
            except client.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in (
                    "404",
                    "NoSuchKey",
                    "NotFound",
                ):
                    raise
            else:
                raise ConnectorError(f"{url} already exists", "pass overwrite=True to replace it")
        with tempfile.TemporaryDirectory(prefix="caliper-export-") as td:
            local = Path(td) / ("out." + fmt)
            n = _write_file(rows, local, fmt, overwrite=True)
            client.upload_file(str(local), bucket, key)
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError(
            "S3 upload failed: " + creds.redact(str(exc), target),
            "check write access to the bucket (s3:PutObject) and the credentials",
        ) from None
    return n


class _PgSink(PostgresConnector):
    read_only = False


class _MySQLSink(MySQLConnector):
    read_only = False


class _SQLiteSink(SQLiteConnector):
    read_only = False


_DB_SINKS = {
    SourceKind.POSTGRES: _PgSink,
    SourceKind.MYSQL: _MySQLSink,
    SourceKind.SQLITE: _SQLiteSink,
}


def _write_table(
    rows: Iterable[dict[str, Any]],
    target: SourceConfig,
    project_dir: Path,
    if_exists: Literal["append", "fail", "replace"],
) -> tuple[int, bool]:
    if not target.table or target.query:
        raise ConnectorError("set target.table for a database export", "e.g. review.labels")
    if if_exists not in ("append", "fail", "replace"):
        raise ConnectorError(f"unknown if_exists '{if_exists}'", "use append, fail or replace")
    if target.kind is SourceKind.SQLITE and target.path:
        (project_dir / Path(target.path).expanduser()).parent.mkdir(parents=True, exist_ok=True)
    conn = _DB_SINKS[target.kind](target, project_dir)
    parts = target.table.split(".")
    schema, table = (parts[-2], parts[-1]) if len(parts) > 1 else (None, parts[-1])
    with tempfile.TemporaryDirectory(prefix="caliper-export-") as td:
        spool = Path(td) / "rows.jsonl"
        n, cols = _spool(rows, spool, flatten=True)
        try:
            con = conn.con()
            q = (
                "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'src' "
                "AND table_name = ?" + (" AND schema_name = ?" if schema else "")
            )
            exists = con.execute(q, [table, *([schema] if schema else [])]).fetchone()[0] > 0
            name = f"src.{qualified(target.table)}"
            if exists and if_exists == "fail":
                raise ConnectorError(
                    f"table {target.table} already exists",
                    "append to it (if_exists='append') or replace it explicitly "
                    "(if_exists='replace')",
                )
            if exists and if_exists == "replace":
                con.execute(f"DROP TABLE {name}")
                exists = False
            if n == 0:
                if not exists:
                    con.execute(f"CREATE TABLE {name} (empty VARCHAR)")
                return 0, not exists
            src = _reader(spool, cols)
            if exists:
                con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM {src}")
            else:
                con.execute(f"CREATE TABLE {name} AS SELECT * FROM {src}")
            return n, not exists
        except ConnectorError:
            raise
        except Exception as exc:
            raise translate(exc, target, f"writing {target.table} failed") from None
        finally:
            conn.close()


def write(
    rows: Iterable[dict[str, Any]],
    target: SourceConfig,
    fmt: str | None = None,
    *,
    project_dir: str | Path | None = None,
    overwrite: bool = False,
    if_exists: Literal["append", "fail", "replace"] = "append",
) -> SinkResult:
    """Write ``rows`` to a file, an S3 object, or a database table (see module docstring)."""
    pdir = Path(project_dir) if project_dir else Path.cwd()
    k = target.kind
    if k is SourceKind.FILE:
        if not target.path:
            raise ConnectorError("target.path is empty", "set the output file path")
        f = _fmt(target.path, fmt, target)
        dest = Path(target.path).expanduser()
        dest = dest if dest.is_absolute() else pdir / dest
        n = _write_file(rows, dest, f, overwrite)
        return SinkResult(target=str(dest), format=f, rows=n)
    if k is SourceKind.S3:
        f = _fmt(target.path, fmt, target)
        return SinkResult(
            target=target.path or "", format=f, rows=_upload_s3(rows, target, f, overwrite)
        )
    if k in _DB_SINKS:
        n, created = _write_table(rows, target, pdir, if_exists)
        return SinkResult(target=target.table or "", format="table", rows=n, created=created)
    raise ConnectorError(
        f"can't export to a '{k.value}' target",
        "export to a file (csv/jsonl/parquet), s3://, or a postgres/mysql/sqlite table",
    )
