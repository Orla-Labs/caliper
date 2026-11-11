# caliper label: product build contract (phase 1b)

The team's working agreement for `label/product`. The spec is
`caliper-plan/03-label-product.md`. If you change anything here, say so in your handoff note.

## Clean-room (hard rule)
Build only from the spec's UX description. Never open, search or reference anything outside this
repo and `caliper-plan/`. That explicitly includes `/Users/mauriciodesouza/Documents/allure` and any
other project on this machine. Public library docs (DuckDB, FastAPI, MDN, WAI-ARIA) are fine.

## Shape
- One process: `caliper label` (FastAPI + plain ES-module frontend, no build step, no Node deps
  shipped). It is local-first, and team mode is the same server behind a token.
- A **project** is a directory holding `caliper.label.yaml` (`project.ProjectConfig`, committed)
  and `.caliper-label/` (state: `project.db` SQLite, snapshots, caches; gitignored).
  `caliper label init` / the wizard writes `.caliper-label/.gitignore` containing `*`.
- Back-compat: `caliper label serve DATA.jsonl --config label.yaml` (old `LabelConfig`) keeps
  working. Legacy configs are converted in memory to a `ProjectConfig` with a
  `file` source (dev 2 writes the adapter). `caliper label export ...` keeps working too.

## CLI (dev 3 owns `cli.py`)
```
caliper label [PROJECT_DIR]            # no dir -> start screen (new/open/demo); dir -> open project
caliper label serve DATA --config CFG  # legacy, unchanged
caliper label init [DIR]               # optional: headless project creation from flags (dev 2)
caliper label export [PROJECT_DIR] --format F --out DEST   # also legacy flags
  common flags: --host 127.0.0.1 --port 8765 --auth token|none --no-browser
```
`--auth token` prints a URL with `?token=...` (generated unless `CALIPER_LABEL_TOKEN` is set).

## Server context
`app.state.ctx: ProjectContext | None` (dev 3 owns `context.py`):
`project_dir: Path`, `config: ProjectConfig`, `store: ProjectStore`, `connector: Connector`,
`version: int` (config revision, bumped on every settings save), plus a `reload()` that the
settings save calls. Routes that need a project return 409 `{"detail": "no project open", "hint": ...}`
when ctx is None.

## Errors
Every `/api` error is JSON `{"detail": str, "hint": str | null}`. `hint` gives the fix in
plain words. Connector errors raise `caliper_label.connectors.ConnectorError(message, hint)`.

## Data layer (dev 1 owns `connectors/`, `store.py`/`project_store.py`, `snapshot.py`, `typedetect.py`, `sinks.py`, `credentials.py`)
```python
class Column(BaseModel): name: str; dtype: str; display: DisplayType  # auto-detected display type
class MediaRef(BaseModel): kind: Literal["file","url"]; path: str | None; url: str | None; mime: str | None

class Connector(Protocol):
    def schema(self) -> list[Column]: ...
    def preview(self, n: int = 50) -> list[dict]: ...
    def count(self) -> int: ...
    def iter_rows(self, batch_size: int = 1000) -> Iterator[list[dict]]: ...
    def resolve_media(self, ref: str) -> MediaRef: ...   # local path (safe), presigned URL, http URL

def open_connector(source: SourceConfig, project_dir: Path) -> Connector   # registry incl. plugins
def credential_status(source: SourceConfig) -> list[CredentialCheck]       # {name, kind, resolved: bool, hint}; never values
```
- `ProjectStore` (SQLite at `.caliper-label/project.db`):
  - snapshot tables: `items(item_id, ordinal, queue, category, golden_answer, display_json, row_hash)`;
  - labels: `LabelRecord` rows plus `project_version`;
  - a snapshot history.
  - Paginated reads: `items(offset, limit, queue=None)`, `item(id)`, `count(queue)`.
  - `labels(...)`, `append_label(rec)`.
  - Never loads everything into memory.
- `snapshot.take(ctx)` / `snapshot.diff(ctx) -> {new, removed, changed}` / `snapshot.apply(diff)`.
- `sinks.write(rows_iter, target: SourceConfig, fmt)`: files (csv/jsonl/parquet), S3 upload, DB table write-back.

## Setup/config (dev 2 owns `project.py` I/O, the `routes_setup.py` + `routes_settings.py` routers, and the `static/js/{start,wizard,settings}/` frontend folders)
- `load_project(dir) -> ProjectConfig`, `save_project(dir, cfg)`: round-trip lossless.
  - UI → YAML → UI is identical.
  - Hand-edited YAML → UI → YAML keeps comments, key order and unknown-but-valid formatting where possible. Use `ruamel.yaml` round-trip mode, applying a diff onto the existing document.
- Label option migrations (rename keeps history; remove → confirm, labels kept but flagged `retired`).
- Templates: Correct/Wrong/Unsure, True/Uncertain/False, Category list (paste), 1–5 rating.

## Labelling, team, export (dev 3 owns `app.py`, `context.py`, `cli.py`, `auth.py`, `routes_label.py`, `routes_team.py`, `routes_export.py`, `export.py`, `agreement.py`, `static/index.html`, `static/js/{label,dashboard,adjudicate,export}/`, `static/css/`)
- Public REST API (documented in OpenAPI at `/docs`): `GET /api/items`, `GET /api/items/{id}`,
  `POST /api/labels`, `GET /api/labels`, `GET /api/next?n=5` (prefetch), `GET /api/media/{item_id}/{field}`.
- Team mode: bearer token (header or `?token=` once, then stored), labeller name, a dashboard
  (per labeller/queue throughput, κ overall and by category, disagreements), and adjudication
  (`source: adjudicated`).
- Export: golden, rollouts, records, csv, jsonl, parquet, DB write-back, S3 upload (via dev 1's `sinks`).
  Every row carries labeller, timestamp, note, source/stage and project version.

## Frontend (no build)
- `static/index.html` loads `/static/css/*.css` and `/static/js/shell.js`.
- `shell.js` routes `#/<route>/...` to `/static/js/<route>/index.js` → `export async function mount(el, ctx)`,
  where `ctx = {api, go, args, query}`. It may return an `unmount` function.
  Routes: `start wizard settings` (dev 2), `label dashboard adjudicate export` (dev 3).
- `static/js/common/api.js` is the fetch wrapper (token and error shape). Shared UI helpers go in
  new files under `static/js/common/`, named by owner prefix if unsure (`common/wizard-*.js` is dev 2's).
  Don't edit another dev's common file; add your own.
- Styling: dev 3 owns `static/css/tokens.css` (colours, spacing, type, focus ring, light/dark) and
  publishes it early. Everyone else uses the tokens and keeps screen-specific CSS in
  `static/css/<route>.css`.
- UX bar (spec §5):
  - keyboard-first, but everything is also clickable;
  - laptop and tablet;
  - under 100 ms between cards (prefetch 5 items and their images);
  - designed empty, error and loading states;
  - visible focus, AA contrast, aria labels.

## Routers
`app.py` includes routers with one line each. Dev 1 adds `routes_data.py`
(`/api/source/*`: test connection, schema, preview, credential status, refresh diff/apply;
`/api/media/...`, unless dev 3 takes it). Dev 2 adds `routes_setup.py` (`/api/projects`, `/api/setup/*`)
and `routes_settings.py` (`/api/settings`). Dev 3 owns everything else. The manager resolves the
one-line include conflicts at merge.

## Tests
- Unit/API tests in `packages/label/tests/test_<area>_*.py`.
- Docker-gated tests are marked `@pytest.mark.docker`. Services are in
  `packages/label/docker/compose.yml` (Postgres, MySQL, MinIO with fictional seed data); run them with
  `docker compose -f packages/label/docker/compose.yml up -d && uv run pytest -m docker`.
- Large-data tests (1M-row Parquet) are marked `@pytest.mark.slow`.
- Both markers are deselected by default.
- Playwright e2e lives in `packages/label/e2e/` and is skipped cleanly without playwright-core.
- Keep green: `uv run pytest -q`, `uv run ruff check packages examples`, `scripts/demo.sh --check`,
  `scripts/demo_eval.sh --check`, and every `scripts/smoke_*.py`.

## Handoff
Notes go in `/Users/mauriciodesouza/Documents/moonshotideas/orlalabs/caliper-wt/handoff/lp-<dev>-*.md`.
