# `caliper label` data connectors

A project reads its items from a **source**: the `source:` block in `caliper.label.yaml`, which the setup wizard writes for you. Every source goes through the same small interface. The built-in connectors run on [DuckDB](https://duckdb.org), so files, object stores and databases all behave the same way: one preview, one row count, one streaming read.

This page covers:
- [Sources](#sources): files, folders of images, S3/GCS/HTTP, Postgres/MySQL/SQLite;
- [Credentials](#credentials): where secrets come from, and why they never reach your files;
- [Snapshots and refresh](#snapshots-and-refresh);
- [Media](#media): images from local paths, S3 or URLs;
- [Exports to files, S3 and tables](#export-sinks);
- [DuckDB extensions and offline use](#duckdb-extensions-and-offline-use);
- [Write your own connector](#write-your-own-connector);
- [Testing against real services](#testing-against-real-services).

## Sources

Relative paths resolve from the project directory. `id_column` names the column that identifies an item. It must be unique and non-empty. Without it, items are numbered `row-0000`, `row-0001`, and so on, which is fine for a file that never changes. Set it whenever you plan to **Refresh from source**.

### Files: csv, tsv, jsonl, json, parquet, xlsx

```yaml
source:
  kind: file
  path: data/tickets.csv          # or an absolute path, or a glob: data/exports/*.parquet
  id_column: ticket_id
  format: csv                     # optional: by default it comes from the extension
  options: {delim: ";", header: true, all_varchar: false, sheet: Sheet1}   # optional reader settings
```

- When the format isn't set, it comes from the extension: `.csv`, `.tsv`/`.tab`, `.jsonl`/`.ndjson`, `.json` (an array or NDJSON), `.parquet`, `.xlsx`.
- Globs read every matching file, with columns matched by name (`union_by_name`).
- An uploaded file is copied into `<project>/data/` and then used like any other path, so the project folder stays self-contained.
- If a CSV column mixes types and DuckDB's type sniffing fails, set `options.all_varchar: true`.

### A folder of images

```yaml
source:
  kind: folder
  path: photos/                   # searched recursively; options.recursive: false for top level only
  sidecar: photos.csv             # optional: joined on file name
```

The folder gives one item per image (png, jpg, jpeg, gif, webp, svg, avif, bmp). Each item has these columns:
- `image`: the path relative to the folder, which is also the default id;
- `file_name`;
- the columns of the sidecar.

The sidecar join key is its `file_name` column. When that column doesn't exist, the first of `filename`, `file`, `image`, `path` or `name` is used; you can also set it with `options.sidecar_key`. A key value that contains a `/` is matched against the relative path instead of the bare file name.

### Object storage: S3 (and S3-compatible), GCS, HTTP(S)

```yaml
source:
  kind: s3
  path: s3://acme-labels/exports/*.parquet
  id_column: item_id
  credentials:
    aws_profile: data-team        # optional; otherwise the default AWS chain is used
    region: eu-west-1             # optional
    endpoint_url: https://<account>.r2.cloudflarestorage.com   # R2 / MinIO / any S3-compatible server
```

```yaml
source: {kind: gcs, path: gs://acme-labels/batch.jsonl}
source: {kind: http, path: https://data.example.com/tickets.csv, credentials: {env: TICKETS_TOKEN}}
```

- **GCS** reads with HMAC keys taken from `GCS_HMAC_KEY_ID` and `GCS_HMAC_SECRET`, or from other variables mapped with `credentials.env_map: {key_id: ..., secret: ...}`. Without HMAC keys, GCS falls back to Application Default Credentials, which needs `pip install gcsfs`.
- **HTTP** sources can send a bearer token taken from the env var named in `credentials.env`.

### Databases: Postgres, MySQL, SQLite

```yaml
source: {kind: postgres, table: support.tickets, id_column: ticket_id}
source: {kind: postgres, query: "select * from support.tickets where created_at > now() - interval '7 days'"}
source: {kind: mysql, table: tickets}
source: {kind: sqlite, path: data/shop.db, table: tickets}       # or query: ...
```

- Give exactly one of `table` (optionally `schema.table`) or `query`.
- Postgres and MySQL queries run on the database itself, in its own SQL dialect, so a preview or count does not pull the whole table.
- SQLite queries run in DuckDB's SQL, against the attached file.
- Every connection is attached **read-only**.

## Credentials

> [!IMPORTANT]
> Credentials never go into project files. `caliper.label.yaml` only *names* where a secret lives. The value is read from the environment when it's needed and kept in process memory only. It is never written to the config, logs, `project.db` or error messages, and every error message is scrubbed of known secret values before anyone sees it.

| Source | Resolved from, in order |
|---|---|
| S3 | `credentials.env_map` (`access_key_id`, `secret_access_key`, `session_token`), then `aws_profile`, then the default AWS chain: env vars, `AWS_PROFILE`, SSO, instance role |
| Postgres | `credentials.env_map`, then `credentials.env` (a DSN env var), then `DATABASE_URL`, then `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` |
| MySQL | `credentials.env_map`, then `credentials.env`, then `DATABASE_URL`, then `MYSQL_HOST`/`MYSQL_TCP_PORT`/`MYSQL_USER`/`MYSQL_PWD`/`MYSQL_DATABASE` |
| GCS | HMAC env vars, then GCP Application Default Credentials (`gcloud auth application-default login`) |
| HTTP | the bearer token in the env var named by `credentials.env` (optional) |

`env_map` maps a connection option to the name of an env var, for example `{password: APP_DB_PASSWORD, host: APP_DB_HOST}`. Host, port, user and database can also go in `source.options`, because they aren't secret.

The wizard's **Test connection** step shows which variable or chain is in use and whether it resolved, never its value. When something fails, it says how to fix it:

```
AWS credentials not found: set AWS_PROFILE or run `aws sso login`, or set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
could not connect to postgres at db.internal:5432: ... (check that Postgres is running and reachable, and PGHOST/PGPORT)
```

In code: `caliper_label.credentials.credential_status(source)` returns a list of `{name, kind, resolved, hint}`.

## Snapshots and refresh

When a project starts, Caliper streams the source in batches into `.caliper-label/project.db`. For each item it keeps:
- the id;
- the queue, category and golden answer, from the configured columns;
- the columns the card shows.

From then on, labelling reads only the snapshot, so it stays stable while the source changes underneath.

**Refresh from source** reads the source again and shows a diff before anything changes:
- **new**: ids that aren't in the snapshot, or that come back after being removed;
- **removed**: ids that are no longer in the source. When the diff is applied, these are flagged, not deleted, and **their labels are kept**;
- **changed**: the displayed fields, queue, category or golden answer changed. Changes to columns the card doesn't show are not counted.

Applying a diff records it in the snapshot history. A diff that has gone stale, because another refresh was applied since, is refused.

API: `POST /api/source/refresh` returns `{diff_id, n_new, n_removed, n_changed, new: [...first 100], ...}`. Page through the ids with `GET /api/source/refresh/{diff_id}/ids?which=removed&offset=100`, then send `POST /api/source/refresh/apply {"diff_id": ...}`.

### Large sources

Every read streams, and every store read is paginated (`items(offset, limit)` or keyset `after=`). DuckDB runs with a 384 MB memory limit and 2 threads, which you can change with `CALIPER_LABEL_DUCKDB_MEMORY_LIMIT` and `CALIPER_LABEL_DUCKDB_THREADS`. Larger sorts and joins spill to `CALIPER_LABEL_DUCKDB_TEMP_DIR`.

Measured on an Apple-silicon laptop with a 1,000,000-row Parquet file (7 columns; `uv run pytest -m slow -s`):

| Step | Time |
|---|---|
| count / preview + schema | 0.02 s / 0.05 s |
| snapshot (`take`) | 11.4 s |
| page at offset 999,950, queue page, keyset page | 0.04 s total |
| refresh diff (no changes) / apply | 15.3 s / 2.3 s |
| peak RSS of the whole run | 254 MB (a 3M-row snapshot peaked at 259 MB) |
| `project.db` size | 689 MB |

Without the memory limit, DuckDB's producer threads run ahead of the reader, and 3M rows reached 966 MB.

## Media

Image fields are served by `GET /api/media/{item_id}/{field}`, where `field` is a card field's index or column name and must be a field of type `image`. The browser never sends a path: the server reads the value from the snapshot and resolves it.

- **`https://...`** answers with a redirect to the URL.
- **`s3://bucket/key`**, or a key relative to an S3 source's folder, answers with a redirect to a **presigned URL valid for 5 minutes**. The URL is signed on the server with the source's credentials. Buckets never need to be public. Change the lifetime with `source.options.presign_seconds`.
- **Local paths** are served only from:
  - the project directory;
  - the source's folder;
  - any folders listed in `source.options.media_roots`.

  Anything else is refused: `..`, absolute paths elsewhere, and symlinks that lead outside these folders. Only image file types are served, with `X-Content-Type-Options: nosniff` and a sandboxing CSP.
- **`gs://`** images are not served yet. Use https URLs instead.

## Export sinks

`caliper_label.sinks.write(rows, target, fmt=None, *, project_dir, overwrite=False, if_exists="append")` writes any iterator of row dicts:

| Target | Behaviour |
|---|---|
| `{kind: file, path: out/labels.parquet}` | csv, jsonl or parquet. An existing file is replaced only with `overwrite=True`. The write goes to a temp file, then an atomic rename. |
| `{kind: s3, path: s3://bucket/labels/run.jsonl}` | Built locally, then uploaded with the same credentials chain used for reading. An existing object is replaced only with `overwrite=True`. |
| `{kind: postgres, table: review.labels}` (also `mysql`, or `sqlite` with `path`) | Creates the table if it is missing, otherwise appends, matching columns by name. `if_exists="fail"` refuses an existing table. **A table is dropped only with `if_exists="replace"`.** |

For csv, parquet and tables, nested values (dicts and lists) are written as JSON text.

## DuckDB extensions and offline use

Formats and sources that need a DuckDB extension:

| Extension | Used for |
|---|---|
| `httpfs` | S3, GCS, HTTP |
| `postgres` | Postgres |
| `mysql` | MySQL |
| `sqlite` | SQLite |
| `excel` | xlsx |

Each extension is downloaded on first use, then cached in `~/.duckdb/extensions`. CSV, JSON and Parquet are built in and never download anything.

Offline, the first use fails with a clear error:

```
DuckDB extension 'postgres' is not installed and could not be downloaded
hint: connect to the internet once so DuckDB can download 'postgres' (it is cached after that), or run
`python -c "import duckdb; duckdb.install_extension('postgres')"` on a connected machine and point
CALIPER_LABEL_DUCKDB_EXTENSION_DIR at the extension folder
```

For air-gapped machines, set one of these:
- `CALIPER_LABEL_DUCKDB_EXTENSION_DIR`: a pre-populated extension directory;
- `CALIPER_LABEL_DUCKDB_EXTENSION_REPO`: a mirror of extensions.duckdb.org.

## Write your own connector

A connector is any object with five methods:

```python
class Connector(Protocol):
    def schema(self) -> list[Column]: ...                  # Column(name, dtype, display)
    def preview(self, n: int = 50) -> list[dict]: ...
    def count(self) -> int: ...
    def iter_rows(self, batch_size: int = 1000) -> Iterator[list[dict]]: ...
    def resolve_media(self, ref: str) -> MediaRef: ...     # MediaRef(kind="file"|"url", path, url, mime)
```

If you subclass `BaseConnector`, only `iter_rows` is required. `preview`, `count`, `schema` (including display-type detection) and `resolve_media` (with safe local paths, presigned S3 URLs and http URLs) all have working defaults. Override them when your backend can do better, for example with a native `COUNT(*)`.

```python
# my_pkg/connector.py
from caliper_label.connectors import BaseConnector, ConnectorError

class WarehouseConnector(BaseConnector):
    def iter_rows(self, batch_size=1000):
        client = make_client(self.source.options["account"])   # options are non-secret settings
        if client is None:
            raise ConnectorError("warehouse not reachable", "check WAREHOUSE_TOKEN is set")
        for page in client.pages(self.source.options["table"], size=batch_size):
            yield [dict(r) for r in page]
```

Register it through an entry point in your package's `pyproject.toml`:

```toml
[project.entry-points."caliper_label.connectors"]
warehouse = "my_pkg.connector:WarehouseConnector"
```

Then use it from a project:

```yaml
source: {kind: plugin, plugin: warehouse, id_column: id, options: {account: acme, table: tickets}}
```

The entry point can be a class or any callable taking `(source, project_dir)`. For tests and notebooks, `caliper_label.connectors.register("warehouse", WarehouseConnector)` does the same without packaging. `available_connectors()` lists the built-in, registered and installed connectors, and the wizard shows these in its source picker.

Rules for connectors:
- **Stream.** Yield batches and never load the whole source.
- **Return JSON-safe values.** `caliper_label.connectors.jsonable()` converts dates, decimals, UUIDs and bytes.
- **Take secrets from the environment only.** Read them at call time, and never put them in messages. Pass any error text through `caliper_label.credentials.redact()`.
- **Raise `ConnectorError(message, hint)`** with a hint that tells the user what to do.

### Snowflake and BigQuery (Phase 2)

These are planned as plugins with the same interface: `caliper-label[snowflake]` and `caliper-label[bigquery]`. They are not shipped yet. Until then, a plugin like the one above can wrap `snowflake-connector-python` or `google-cloud-bigquery`. Another option is to export to Parquet on S3 or GCS and use the built-in connectors.

## Testing against real services

`packages/label/docker/compose.yml` runs Postgres 16, MySQL 8.4 and an S3-compatible server, all seeded with fictional data:

```bash
docker compose -f packages/label/docker/compose.yml -p caliper-label-test up -d --wait
uv run pytest -m docker
docker compose -f packages/label/docker/compose.yml -p caliper-label-test down -v
```

- **Ports.** The defaults are 55432 (Postgres), 53306 (MySQL) and 59000/59001 (S3). If one is taken, set `CALIPER_LABEL_PG_PORT`, `CALIPER_LABEL_MYSQL_PORT` or `CALIPER_LABEL_S3_PORT` for both commands.
- **S3 server.** It defaults to RustFS, because MinIO's images can no longer be pulled anonymously. If you have a MinIO image, use `CALIPER_LABEL_S3_IMAGE=minio/minio:latest`; the tests pass against both.
- **Large-data test.** The 1M-row test runs with `uv run pytest -m slow -s`.
