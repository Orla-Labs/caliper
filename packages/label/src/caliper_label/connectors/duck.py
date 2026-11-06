"""Built-in connectors on DuckDB: files, folders of images, S3/GCS/HTTP, Postgres, MySQL, SQLite.

Each connector builds one SQL *relation* (``read_parquet('...')``, ``src.public.tickets``,
``postgres_query('src', '...')``) and runs ``schema``/``preview``/``count``/``iter_rows`` against
it on a private in-memory DuckDB. Rows stream with ``fetchmany``; nothing is materialised.

Extensions (httpfs, postgres, mysql, sqlite, excel) are loaded on first use and downloaded if
missing. Offline, that fails with a ConnectorError that says what to do. Two env vars help in
air-gapped setups: ``CALIPER_LABEL_DUCKDB_EXTENSION_DIR`` (a pre-populated extension directory)
and ``CALIPER_LABEL_DUCKDB_EXTENSION_REPO`` (a mirror of extensions.duckdb.org).

Secrets go into DuckDB as TEMPORARY secrets (process memory only) and every error is passed
through ``credentials.redact`` before it becomes a ConnectorError.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb

from .. import credentials as creds
from ..project import SourceConfig, SourceKind
from ..typedetect import IMAGE_EXTENSIONS, detect_columns
from .base import BaseConnector, Column, ConnectorError, jsonable

FORMATS = ("csv", "tsv", "jsonl", "json", "parquet", "xlsx")
_EXT_FORMAT = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".json": "json",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".xlsx": "xlsx",
}
SCHEMA_SAMPLE = 200


def lit(s: str) -> str:
    """A SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def ident(s: str) -> str:
    return '"' + str(s).replace('"', '""') + '"'


def qualified(name: str) -> str:
    """``schema.table`` -> ``"schema"."table"`` (already-quoted names are kept)."""
    if '"' in name:
        return name
    return ".".join(ident(p) for p in name.split("."))


def _opt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    return lit(str(v))


# --------------------------------------------------------------------------- engine


def _new_connection() -> duckdb.DuckDBPyConnection:
    cfg: dict[str, Any] = {}
    ext_dir = os.environ.get("CALIPER_LABEL_DUCKDB_EXTENSION_DIR")
    if ext_dir:
        cfg["extension_directory"] = ext_dir
    repo = os.environ.get("CALIPER_LABEL_DUCKDB_EXTENSION_REPO")
    if repo:
        cfg["custom_extension_repository"] = repo
        cfg["autoinstall_extension_repository"] = repo
    con = duckdb.connect(":memory:", config=cfg)
    _utc(con)
    return con


def _utc(con: duckdb.DuckDBPyConnection) -> None:
    """Row hashes must not depend on the machine's zone. A session setting: set per cursor."""
    try:
        con.execute("SET TimeZone = 'UTC'")
    except duckdb.Error:  # icu unavailable: TIMESTAMPTZ then renders in UTC offsets anyway
        pass


def load_extension(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """LOAD, else INSTALL then LOAD; offline -> ConnectorError with a fix."""
    try:
        con.load_extension(name)
        return
    except duckdb.Error:
        pass
    try:
        con.install_extension(name)
        con.load_extension(name)
    except duckdb.Error as exc:
        raise ConnectorError(
            f"DuckDB extension '{name}' is not installed and could not be downloaded "
            f"({type(exc).__name__})",
            f"connect to the internet once so DuckDB can download '{name}' (it is cached after "
            f"that), or run `python -c \"import duckdb; duckdb.install_extension('{name}')\"` "
            "on a connected machine and point CALIPER_LABEL_DUCKDB_EXTENSION_DIR at the "
            "extension folder",
        ) from None


_CONNECT_HINTS = {
    SourceKind.POSTGRES: "check that Postgres is running and reachable, and PGHOST/PGPORT "
    "(or the host and port in DATABASE_URL)",
    SourceKind.MYSQL: "check that MySQL is running and reachable, and MYSQL_HOST/MYSQL_TCP_PORT "
    "(or the host and port in DATABASE_URL)",
    SourceKind.S3: "check the network and credentials.endpoint_url (S3-compatible) or "
    "credentials.region (AWS)",
    SourceKind.GCS: "check the network connection to storage.googleapis.com",
    SourceKind.HTTP: "check the URL and that the server is reachable from this machine",
}


def translate(exc: Exception, source: SourceConfig | None, what: str = "") -> ConnectorError:
    """DuckDB/driver exception -> ConnectorError with secrets removed and a hint."""
    if isinstance(exc, ConnectorError):
        return exc
    msg = creds.redact(str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc), source)
    low = msg.lower()
    kind = source.kind if source else None
    hint: str | None = None
    if "no files found" in low or "no such file" in low or "cannot open file" in low:
        hint = "check the path; relative paths are resolved from the project folder"
    elif "403" in low or "forbidden" in low or "access denied" in low or "401" in low:
        if kind is SourceKind.S3:
            hint = (
                "the credentials can't read this object: check the bucket policy, "
                "AWS_PROFILE, or run `aws sso login`"
            )
        else:
            hint = "the server refused access: check the credentials env vars"
    elif any(k in low for k in ("could not connect", "connection refused", "can't connect")):
        hint = _CONNECT_HINTS.get(kind, "check the address and that the server is reachable")
    elif "password authentication failed" in low or "access denied for user" in low:
        hint = "the database rejected the user/password: check the env vars that hold them"
    elif "catalog error" in low or "does not exist" in low or "doesn't exist" in low:
        hint = "check the table name (schema.table) or the SQL query"
    elif "parser error" in low or "syntax error" in low:
        hint = "fix the SQL query"
    elif "conversion error" in low or "could not convert" in low:
        hint = "a column has mixed types; set source.options.all_varchar: true to read text"
    elif "http" in low and ("404" in low or "not found" in low):
        hint = "the URL or object does not exist"
    prefix = f"{what}: " if what else ""
    return ConnectorError(prefix + msg, hint)


class DuckDBConnector(BaseConnector):
    """Common engine. Subclasses implement ``relation()`` and maybe ``setup()``/``native_*``."""

    extensions: tuple[str, ...] = ()
    default_id_column: str | None = None
    read_only = True  # databases are ATTACHed READ_ONLY; sinks.py flips this for write-back
    session_sql: tuple[str, ...] = ()  # per-cursor statements (USE ...); cursors don't inherit

    def _ro(self) -> str:
        return ", READ_ONLY" if self.read_only else ""

    def __init__(self, source: SourceConfig, project_dir: Path | None = None) -> None:
        super().__init__(source, project_dir)
        self._lock = threading.RLock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._dtypes: dict[str, str] | None = None

    # -- subclass hooks ---------------------------------------------------------------
    def relation(self) -> str:
        raise NotImplementedError

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create secrets / ATTACH. Runs once per connector."""

    def preview_sql(self, n: int) -> str:
        return f"SELECT * FROM {self.relation()} LIMIT {int(n)}"

    def count_sql(self) -> str:
        return f"SELECT count(*) FROM {self.relation()}"

    # -- engine -----------------------------------------------------------------------
    @property
    def base_dir(self) -> Path:
        return self.project_dir or Path.cwd()

    def local_path(self, p: str) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*://", p, re.I):
            return p
        pp = Path(p).expanduser()
        return str(pp if pp.is_absolute() else self.base_dir / pp)

    def con(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._con is None:
                con = _new_connection()
                try:
                    for ext in self.extensions:
                        load_extension(con, ext)
                    self.setup(con)
                except ConnectorError:
                    con.close()
                    raise
                except Exception as exc:
                    con.close()
                    raise translate(exc, self.source, "connection failed") from None
                self._con = con
            return self._con

    def _run(self, sql: str) -> duckdb.DuckDBPyConnection:
        cur = self.con().cursor()
        try:
            _utc(cur)
            for stmt in self.session_sql:
                cur.execute(stmt)
            cur.execute(sql)
        except Exception as exc:
            cur.close()
            raise translate(exc, self.source) from None
        return cur

    def dtypes(self) -> dict[str, str]:
        if self._dtypes is None:
            cur = self._run(f"DESCRIBE {self.preview_sql(0)}")
            try:
                self._dtypes = {r[0]: str(r[1]) for r in cur.fetchall()}
            finally:
                cur.close()
        return self._dtypes

    def _converter(self, names: list[str]):
        dt = self.dtypes()
        json_cols = {i for i, n in enumerate(names) if dt.get(n, "").upper() == "JSON"}

        def conv(row: tuple) -> dict[str, Any]:
            out = {}
            for i, (n, v) in enumerate(zip(names, row, strict=True)):
                if i in json_cols and isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                out[n] = jsonable(v)
            return out

        return conv

    def _rows(self, sql: str, batch_size: int) -> Iterator[list[dict[str, Any]]]:
        cur = self._run(sql)
        try:
            names = [d[0] for d in cur.description]
            conv = self._converter(names)
            while True:
                try:
                    chunk = cur.fetchmany(batch_size)
                except Exception as exc:
                    raise translate(exc, self.source) from None
                if not chunk:
                    return
                yield [conv(r) for r in chunk]
        finally:
            cur.close()

    # -- Connector --------------------------------------------------------------------
    def schema(self) -> list[Column]:
        rows = self.preview(SCHEMA_SAMPLE)
        return detect_columns(list(self.dtypes().items()), rows)

    def preview(self, n: int = 50) -> list[dict[str, Any]]:
        return [r for b in self._rows(self.preview_sql(n), max(n, 1)) for r in b][:n]

    def count(self) -> int:
        cur = self._run(self.count_sql())
        try:
            return int(cur.fetchone()[0])
        finally:
            cur.close()

    def iter_rows(self, batch_size: int = 1000) -> Iterator[list[dict[str, Any]]]:
        yield from self._rows(f"SELECT * FROM {self.relation()}", batch_size)

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None


# --------------------------------------------------------------------------- files


def detect_format(path: str, explicit: str | None) -> str:
    if explicit:
        f = explicit.lower().lstrip(".")
        if f not in FORMATS:
            raise ConnectorError(
                f"unknown format '{explicit}'", f"use one of: {', '.join(FORMATS)}"
            )
        return f
    name = path.split("?", 1)[0].rstrip("/")
    suffix = Path(name).suffix.lower()
    if suffix == ".gz":
        suffix = Path(name[:-3]).suffix.lower()
    if suffix in _EXT_FORMAT:
        return _EXT_FORMAT[suffix]
    raise ConnectorError(
        f"can't tell the file format of '{path}'",
        f"set source.format to one of: {', '.join(FORMATS)}",
    )


_READER_OPTS = {
    "csv": {"delim", "delimiter", "header", "quote", "escape", "skip", "all_varchar", "encoding"},
    "tsv": {"header", "quote", "escape", "skip", "all_varchar", "encoding"},
    "jsonl": {"maximum_object_size", "ignore_errors"},
    "json": {"maximum_object_size", "ignore_errors"},
    "parquet": set(),
    "xlsx": {"sheet", "header", "all_varchar", "range"},
}


def file_reader(path: str, fmt: str, options: dict[str, Any]) -> str:
    """DuckDB table function call that reads ``path`` (a file, URL or glob)."""
    opts = {k: v for k, v in options.items() if k in _READER_OPTS[fmt]}
    if fmt in ("csv", "tsv", "jsonl", "json", "parquet") and any(c in path for c in "*?["):
        opts.setdefault("union_by_name", True)
    if fmt == "csv":
        fn = "read_csv"
    elif fmt == "tsv":
        fn = "read_csv"
        opts["delim"] = "\t"
    elif fmt == "jsonl":
        fn = "read_json"
        opts["format"] = "newline_delimited"
    elif fmt == "json":
        fn = "read_json"
        opts.setdefault("format", "auto")
    elif fmt == "parquet":
        fn = "read_parquet"
    else:
        fn = "read_xlsx"
    args = "".join(f", {k} = {_opt_value(v)}" for k, v in opts.items())
    return f"{fn}({lit(path)}{args})"


class FileConnector(DuckDBConnector):
    """csv/tsv/jsonl/json/parquet/xlsx from a local path, glob, or (subclasses) a remote URL."""

    def __init__(self, source: SourceConfig, project_dir: Path | None = None) -> None:
        super().__init__(source, project_dir)
        if not source.path:
            raise ConnectorError("source.path is empty", "set the file path or URL to read")
        self.fmt = detect_format(source.path, source.format)
        self.extensions = self.extensions + (("excel",) if self.fmt == "xlsx" else ())

    def target(self) -> str:
        return self.local_path(self.source.path)  # type: ignore[arg-type]

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        t = self.target()
        if "://" not in t and not any(c in t for c in "*?[") and not Path(t).exists():
            raise ConnectorError(
                f"file not found: {self.source.path}",
                "check the path; relative paths are resolved from the project folder",
            )

    def relation(self) -> str:
        return file_reader(self.target(), self.fmt, self.source.options)


# --------------------------------------------------------------------------- folder of images


class FolderConnector(DuckDBConnector):
    """One item per image file under ``path`` (recursive), plus an optional sidecar CSV.

    Columns: ``image`` (path relative to the folder, served via resolve_media), ``file_name``,
    then the sidecar's columns. The sidecar joins on its ``file_name`` column (or
    ``options.sidecar_key``) against the image's file name, or its relative path when the sidecar
    value contains a ``/``.
    """

    default_id_column = "image"

    def __init__(self, source: SourceConfig, project_dir: Path | None = None) -> None:
        super().__init__(source, project_dir)
        if not source.path:
            raise ConnectorError("source.path is empty", "set the folder that holds the images")
        self.folder = Path(self.local_path(source.path)).resolve()
        if not self.folder.is_dir():
            raise ConnectorError(f"folder not found: {source.path}", "check the folder path")
        self.sidecar = Path(self.local_path(source.sidecar)) if source.sidecar else None
        if self.sidecar is not None and not self.sidecar.is_file():
            raise ConnectorError(f"sidecar not found: {source.sidecar}", "check the sidecar path")

    def _images(self) -> str:
        pattern = str(self.folder / ("**/*" if self.source.options.get("recursive", True) else "*"))
        exts = "|".join(e.lstrip(".") for e in IMAGE_EXTENSIONS)
        n = len(str(self.folder)) + 2
        return (
            f"(SELECT substr(file, {n}) AS image, parse_filename(file) AS file_name "
            f"FROM glob({lit(pattern)}) "
            f"WHERE regexp_matches(lower(file), {lit(rf'\.({exts})$')}))"
        )

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        if self.sidecar is None:
            return
        fmt = detect_format(str(self.sidecar), None)
        reader = file_reader(str(self.sidecar), fmt, self.source.options)
        try:
            cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()]
        except Exception as exc:
            raise translate(exc, self.source, "sidecar") from None
        key = self.source.options.get("sidecar_key")
        if key is None:
            key = next(
                (
                    c
                    for c in ("file_name", "filename", "file", "image", "path", "name")
                    if c in cols
                ),
                cols[0] if cols else None,
            )
        if key not in cols:
            raise ConnectorError(
                f"sidecar has no column '{key}'",
                f"set source.options.sidecar_key to one of: {', '.join(cols)}",
            )
        self._sidecar_sql = reader
        self._sidecar_key = key
        self._sidecar_cols = [c for c in cols if c != key and c not in ("image", "file_name")]

    def relation(self) -> str:
        self.con()
        if self.sidecar is None:
            return f"(SELECT * FROM {self._images()} ORDER BY image)"
        k = ident(self._sidecar_key)
        extra = "".join(f", s.{ident(c)}" for c in self._sidecar_cols)
        return (
            f"(SELECT i.image, i.file_name{extra} FROM {self._images()} i "
            f"LEFT JOIN {self._sidecar_sql} s ON CAST(s.{k} AS VARCHAR) = "
            f"CASE WHEN contains(CAST(s.{k} AS VARCHAR), '/') THEN i.image ELSE i.file_name END "
            "ORDER BY i.image)"
        )

    def media_roots(self) -> list[Path]:
        return [self.folder, *super().media_roots()]


# --------------------------------------------------------------------------- remote files


class S3Connector(FileConnector):
    """``s3://bucket/key`` or globs; S3-compatible endpoints via credentials.endpoint_url."""

    extensions = ("httpfs",)

    def target(self) -> str:
        p = self.source.path or ""
        if p.startswith("r2://"):
            p = "s3://" + p[5:]
        if not p.startswith("s3://"):
            raise ConnectorError(f"not an S3 URL: {p}", "use s3://bucket/path/file.parquet")
        return p

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        c = creds.aws_credentials(self.source)
        parts = ["TYPE s3", f"KEY_ID {lit(c.key_id)}", f"SECRET {lit(c.secret)}"]
        if c.token:
            parts.append(f"SESSION_TOKEN {lit(c.token)}")
        if c.region:
            parts.append(f"REGION {lit(c.region)}")
        ep = self.source.credentials.endpoint_url
        if ep:
            host = re.sub(r"^https?://", "", ep).rstrip("/")
            parts += [
                f"ENDPOINT {lit(host)}",
                "URL_STYLE 'path'",
                f"USE_SSL {'false' if ep.startswith('http://') else 'true'}",
            ]
        try:
            con.execute(f"CREATE OR REPLACE TEMPORARY SECRET caliper_s3 ({', '.join(parts)})")
        except Exception as exc:
            raise translate(exc, self.source, "S3 setup") from None


class GCSConnector(FileConnector):
    """``gs://bucket/key``: HMAC keys via env (DuckDB-native), else ADC via gcsfs if installed."""

    extensions = ("httpfs",)

    def target(self) -> str:
        p = self.source.path or ""
        if p.startswith("gcs://"):
            p = "gs://" + p[6:]
        if not p.startswith("gs://"):
            raise ConnectorError(f"not a GCS URL: {p}", "use gs://bucket/path/file.parquet")
        return p

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        hmac = creds.gcs_hmac(self.source)
        if hmac:
            con.execute(
                "CREATE OR REPLACE TEMPORARY SECRET caliper_gcs "
                f"(TYPE gcs, KEY_ID {lit(hmac[0])}, SECRET {lit(hmac[1])})"
            )
            return
        try:
            import gcsfs  # type: ignore[import-not-found]
        except ImportError:
            raise ConnectorError(
                "GCS credentials not found",
                "set GCS_HMAC_KEY_ID and GCS_HMAC_SECRET (HMAC keys), or `pip install gcsfs` "
                "and run `gcloud auth application-default login`",
            ) from None
        con.register_filesystem(gcsfs.GCSFileSystem())  # pragma: no cover - needs gcsfs


class HttpConnector(FileConnector):
    """``https://host/file.csv``; optional bearer token from the env var in credentials.env."""

    extensions = ("httpfs",)

    def target(self) -> str:
        p = self.source.path or ""
        if not p.lower().startswith(("http://", "https://")):
            raise ConnectorError(f"not an HTTP(S) URL: {p}", "use https://host/path/file.csv")
        return p

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        var = self.source.credentials.env
        if not var:
            return
        token = os.environ.get(var)
        if not token:
            raise ConnectorError(f"{var} is not set", f"export {var}=<bearer token>")
        con.execute(
            "CREATE OR REPLACE TEMPORARY SECRET caliper_http (TYPE http, "
            f"EXTRA_HTTP_HEADERS MAP {{'Authorization': {lit('Bearer ' + token)}}})"
        )


# --------------------------------------------------------------------------- databases


def _db_target(source: SourceConfig) -> tuple[str | None, str | None]:
    if bool(source.table) == bool(source.query):
        raise ConnectorError(
            "set exactly one of source.table or source.query", "pick a table, or write a query"
        )
    q = source.query.strip().rstrip(";").strip() if source.query else None
    return source.table, q


class _ServerDbConnector(DuckDBConnector):
    """Postgres/MySQL: ATTACH read-only through a temporary secret; push LIMIT/COUNT down."""

    ext: str = ""
    db_type: str = ""
    query_fn: str = ""

    def __init__(self, source: SourceConfig, project_dir: Path | None = None) -> None:
        super().__init__(source, project_dir)
        self.extensions = (self.ext,)
        self.table, self.query = _db_target(source)

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        p = creds.db_params(self.source)
        parts = [f"TYPE {self.db_type}"]
        for k, v in (
            ("HOST", p.host),
            ("PORT", p.port),
            ("DATABASE", p.database),
            ("USER", p.user),
            ("PASSWORD", p.password),
        ):
            if v:
                parts.append(f"{k} {lit(v)}")
        extra = " ".join(f"{k}={v}" for k, v in p.extra if re.fullmatch(r"[a-z_]+", k))
        try:
            con.execute(f"CREATE OR REPLACE TEMPORARY SECRET caliper_db ({', '.join(parts)})")
            con.execute(
                f"ATTACH {lit(extra)} AS src (TYPE {self.db_type}, SECRET caliper_db{self._ro()})"
            )
        except Exception as exc:
            where = f"{p.host or 'localhost'}:{p.port or 'default port'}"
            raise translate(
                exc, self.source, f"could not connect to {self.db_type} at {where}"
            ) from None

    def native(self, sql: str) -> str:
        return f"{self.query_fn}('src', {lit(sql)})"

    def _inner(self) -> str:
        return f"({self.query}) AS q" if self.query else self._remote_name()

    def _remote_name(self) -> str:
        return ".".join(self._q(p) for p in (self.table or "").split("."))

    def _q(self, part: str) -> str:
        return '"' + part.replace('"', '""') + '"'

    def relation(self) -> str:
        if self.query:
            return self.native(self.query)
        return f"src.{qualified(self.table or '')}"

    def preview_sql(self, n: int) -> str:
        return f"SELECT * FROM {self.native(f'SELECT * FROM {self._inner()} LIMIT {int(n)}')}"

    def count_sql(self) -> str:
        return f"SELECT * FROM {self.native(f'SELECT count(*) FROM {self._inner()}')}"


class PostgresConnector(_ServerDbConnector):
    ext = "postgres"
    db_type = "postgres"
    query_fn = "postgres_query"


class MySQLConnector(_ServerDbConnector):
    ext = "mysql"
    db_type = "mysql"
    query_fn = "mysql_query"

    def _q(self, part: str) -> str:
        return "`" + part.replace("`", "``") + "`"


class SQLiteConnector(DuckDBConnector):
    extensions = ("sqlite",)
    session_sql = ("USE src",)  # a query's bare table names resolve in the SQLite file

    def __init__(self, source: SourceConfig, project_dir: Path | None = None) -> None:
        super().__init__(source, project_dir)
        if not source.path:
            raise ConnectorError("source.path is empty", "set the SQLite database file")
        self.table, self.query = _db_target(source)
        self.db_path = Path(self.local_path(source.path))
        if self.read_only and not self.db_path.is_file():
            raise ConnectorError(f"SQLite file not found: {source.path}", "check the path")

    def setup(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute(f"ATTACH {lit(str(self.db_path))} AS src (TYPE sqlite{self._ro()})")

    def relation(self) -> str:
        if self.query:
            return f"({self.query})"
        return f"src.{qualified(self.table or '')}"


# --------------------------------------------------------------------------- uploads


def save_upload(dest_dir: Path, filename: str, fileobj, max_bytes: int | None = None) -> Path:
    """Copy an uploaded file into ``dest_dir`` under a safe name; returns the new path.

    The wizard stores uploads in ``<project>/data/`` and then uses a ``file`` source with that
    (relative) path, so the project directory stays self-contained.
    """
    name = Path(filename or "upload").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".") or "upload"
    if Path(name).suffix.lower() not in _EXT_FORMAT:
        raise ConnectorError(
            f"unsupported upload type: {name}", f"upload one of: {', '.join(sorted(_EXT_FORMAT))}"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = dest_dir / f"{stem}-{n}{suffix}"
        n += 1
    written = 0
    with target.open("wb") as out:
        while chunk := fileobj.read(1 << 20):
            written += len(chunk)
            if max_bytes is not None and written > max_bytes:
                out.close()
                target.unlink(missing_ok=True)
                raise ConnectorError("upload too large", f"files up to {max_bytes} bytes")
            out.write(chunk)
    return target


BUILTINS: dict[SourceKind, type[DuckDBConnector]] = {
    SourceKind.FILE: FileConnector,
    SourceKind.FOLDER: FolderConnector,
    SourceKind.S3: S3Connector,
    SourceKind.GCS: GCSConnector,
    SourceKind.HTTP: HttpConnector,
    SourceKind.POSTGRES: PostgresConnector,
    SourceKind.MYSQL: MySQLConnector,
    SourceKind.SQLITE: SQLiteConnector,
}
