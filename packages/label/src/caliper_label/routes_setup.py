"""Start screen + setup wizard API (dev 2): `/api/projects`, `/api/setup/*`.

Projects
    GET  /api/projects                 recent projects, demo availability, a default parent folder
    POST /api/projects/open            {path}           open an existing project
    POST /api/projects/forget          {path}           drop from the recent list
    POST /api/projects/demo            {reset?}         open the fictional demo project
Wizard
    GET  /api/setup/templates                           label templates
    POST /api/setup/templates/{id}     {categories?}    -> LabelsConfig
    GET  /api/setup/connectors                          source kinds incl. plugins
    PUT  /api/setup/upload?filename=&draft=  (raw body) -> {draft, path, size}
    POST /api/setup/preview            {source, draft?, n?}  credentials + schema + rows + count
    POST /api/setup/validate           {config}          -> {issues, yaml}
    GET  /api/setup/browse?path=&files=                  folder picker (local requests only)
    POST /api/setup/check-dir          {dir}             is the folder free / inside a git checkout?
    POST /api/setup/create             {dir, config, draft?, overwrite?}

Opening a project goes through `app.state.open_project(project_dir)` (dev 3's context: it
must set `app.state.ctx`). Uploads are staged under `<home>/drafts/<draft>/data/` and moved into
`<project>/data/` on create, so the project dir stays self-contained. `<home>` is
`$CALIPER_LABEL_HOME` or `~/.caliper-label`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from .project import (
    CONFIG_FILENAME,
    STATE_DIRNAME,
    ProjectConfig,
    ProjectFileError,
    SourceConfig,
    SourceKind,
    _short_errors,
    load_project,
    render_project,
    save_project,
)
from .project_setup import TEMPLATES, Issue, errors, template_labels, validate_project

MAX_UPLOAD = int(os.environ.get("CALIPER_LABEL_MAX_UPLOAD", str(2 << 30)))  # 2 GiB
MAX_RECENT = 12
DATA_EXT = {".csv", ".tsv", ".jsonl", ".ndjson", ".json", ".parquet", ".xlsx", ".sqlite", ".db"}
_DRAFT_RE = re.compile(r"^[a-z0-9]{8,32}$")


class SetupError(HTTPException):
    def __init__(self, status: int, detail: str, hint: str | None = None) -> None:
        super().__init__(status, detail)
        self.hint = hint


class ErrorShapeRoute(APIRoute):
    """Every error from these routers is `{"detail": str, "hint": str | null}` (CONTRACT),
    whatever exception handlers the app installs."""

    def get_route_handler(self):  # type: ignore[no-untyped-def]
        inner = super().get_route_handler()

        async def handler(request: Request):  # type: ignore[no-untyped-def]
            try:
                return await inner(request)
            except SetupError as exc:
                body = {"detail": exc.detail, "hint": exc.hint}
                return JSONResponse(body, status_code=exc.status_code)
            except ProjectFileError as exc:
                return JSONResponse({"detail": str(exc), "hint": exc.hint}, status_code=422)
            except RequestValidationError as exc:
                parts = []
                for e in exc.errors():
                    loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
                    parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
                return JSONResponse(
                    {"detail": "; ".join(parts), "hint": "check the request body"},
                    status_code=422,
                )

        return handler


router = APIRouter(tags=["setup"], route_class=ErrorShapeRoute)


# --------------------------------------------------------------------------- home / recents


def home() -> Path:
    return Path(os.environ.get("CALIPER_LABEL_HOME") or Path.home() / ".caliper-label").expanduser()


def _recent_file() -> Path:
    return home() / "recent.json"


def read_recent() -> list[dict[str, Any]]:
    try:
        data = json.loads(_recent_file().read_text(encoding="utf-8"))
        return [r for r in data if isinstance(r, dict) and "path" in r]
    except (OSError, ValueError):
        return []


def remember(project_dir: Path, name: str) -> None:
    """Put a project at the top of the recent list (atomic write)."""
    path = str(Path(project_dir).resolve())
    rows = [r for r in read_recent() if r.get("path") != path]
    rows.insert(0, {"path": path, "name": name, "opened_at": time.time()})
    f = _recent_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[:MAX_RECENT], indent=1), encoding="utf-8")
    tmp.replace(f)


def _prefs_file() -> Path:
    return home() / "prefs.json"


def read_prefs() -> dict[str, Any]:
    try:
        data = json.loads(_prefs_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_prefs(**updates: Any) -> None:
    prefs = {**read_prefs(), **updates}
    f = _prefs_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
    tmp.replace(f)


def git_root(path: Path) -> Path | None:
    """The git checkout that contains `path` (a `.git` dir, or a `.git` file in worktrees)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    for cand in (p, *p.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def default_parent() -> Path:
    """Where new projects go by default.

    The last parent folder a project was created in, else `$CALIPER_LABEL_PROJECTS`, else
    `~/caliper-projects`. Never inside the git checkout the server was started from (the
    usual case is the Caliper source tree itself).
    """
    cwd_git = git_root(Path.cwd())
    env = os.environ.get("CALIPER_LABEL_PROJECTS")
    candidates = [read_prefs().get("last_parent"), env, str(Path.home() / "caliper-projects")]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if not p.is_absolute():
            continue
        if cwd_git is not None and _inside(p, cwd_git):
            continue
        return p
    return Path.home() / "caliper-projects"


def forget(path: str) -> None:
    rows = [r for r in read_recent() if r.get("path") != path]
    f = _recent_file()
    if f.exists():
        f.write_text(json.dumps(rows, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- helpers


def _local_only(request: Request) -> None:
    """Filesystem browsing and creating projects are for the person at this machine.

    On by default in team mode (an auth token is set); `app.state.setup_local_only` overrides.
    """
    state = request.app.state
    local_only = getattr(state, "setup_local_only", None)
    if local_only is None:
        local_only = bool(getattr(state, "auth_token", None))
    if not local_only:
        return
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise SetupError(
            403,
            "project setup is only available on the machine running caliper label",
            "open the start screen on that machine, or run `caliper label init` there",
        )


def _default_opener(request: Request) -> Any:
    """Dev 3's context: ProjectContext.open + set_ctx (closing the previous project's store)."""
    try:
        from .context import ProjectContext, set_ctx
    except ImportError:  # pragma: no cover - context.py not merged
        return None

    def opener(project_dir: Path) -> Any:
        old = getattr(request.app.state, "ctx", None)
        ctx = ProjectContext.open(project_dir)
        set_ctx(request.app, ctx)
        if old is not None and getattr(old, "store", None) is not None:
            with contextlib.suppress(Exception):
                old.store.close()
        return ctx

    return opener


def _open(request: Request, project_dir: Path) -> Any:
    if hasattr(request.app.state, "open_project"):
        opener = request.app.state.open_project
    else:
        opener = _default_opener(request)
    if opener is None:
        raise SetupError(
            501,
            "this server cannot switch projects",
            f"restart with `caliper label {project_dir}`",
        )
    try:
        ctx = opener(project_dir)
    except ProjectFileError as exc:
        raise SetupError(422, str(exc), exc.hint) from None
    except HTTPException:
        raise
    except Exception as exc:  # connector errors etc. carry a hint
        hint = getattr(exc, "hint", None)
        if hint is None and not hasattr(exc, "message"):
            raise
        raise SetupError(422, getattr(exc, "message", None) or str(exc), hint) from None
    return ctx


def _project_summary(project_dir: Path, cfg: ProjectConfig) -> dict[str, Any]:
    return {
        "path": str(project_dir),
        "name": cfg.name,
        "config": str(project_dir / CONFIG_FILENAME),
    }


def _draft_dir(draft: str | None) -> Path | None:
    if not draft:
        return None
    if not _DRAFT_RE.match(draft):
        raise SetupError(400, "bad draft id")
    d = home() / "drafts" / draft
    return d if d.is_dir() else None


def _issues_from_validation(exc: ValidationError) -> list[Issue]:
    out = []
    for err in exc.errors():
        out.append(
            Issue(
                path=".".join(str(x) for x in err["loc"]),
                message=err["msg"].removeprefix("Value error, "),
            )
        )
    return out


def _parse_config(data: Any) -> tuple[ProjectConfig | None, list[Issue]]:
    try:
        cfg = ProjectConfig.model_validate(data)
    except ValidationError as exc:
        return None, _issues_from_validation(exc)
    return cfg, validate_project(cfg)


# --------------------------------------------------------------------------- demo


def demo_dir() -> Path | None:
    """examples/label_demo in a source checkout (`CALIPER_LABEL_DEMO` overrides)."""
    env = os.environ.get("CALIPER_LABEL_DEMO")
    if env:
        p = Path(env)
        return p if (p / CONFIG_FILENAME).exists() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "examples" / "label_demo"
        if (cand / CONFIG_FILENAME).exists():
            return cand
    return None


def prepare_demo(reset: bool = False) -> Path:
    """Copy the demo project into `<home>/demo` (examples/ stays clean), absolutising paths."""
    src = demo_dir()
    if src is None:
        raise SetupError(
            404,
            "the demo project is not available in this install",
            "run caliper label from a Caliper source checkout, or set CALIPER_LABEL_DEMO",
        )
    dest = home() / "demo"
    if reset and dest.exists():
        shutil.rmtree(dest)
    if not (dest / CONFIG_FILENAME).exists():
        dest.mkdir(parents=True, exist_ok=True)
        text = (src / CONFIG_FILENAME).read_text(encoding="utf-8")
        cfg = load_project(src)
        base = src.resolve()
        upd: dict[str, Any] = {}
        if cfg.source.path and not re.match(r"^[a-z0-9]+://", cfg.source.path):
            upd["path"] = str((base / cfg.source.path).resolve())
        roots = cfg.source.options.get("media_roots") or []
        opts = dict(cfg.source.options)
        if roots:
            opts["media_roots"] = [str((base / r).resolve()) for r in roots]
            upd["options"] = opts
        cfg = cfg.model_copy(update={"source": cfg.source.model_copy(update=upd)})
        (dest / CONFIG_FILENAME).write_text(render_project(cfg, text), encoding="utf-8")
        save_project(dest, cfg)  # writes .caliper-label/.gitignore
    return dest


# --------------------------------------------------------------------------- projects


class PathIn(BaseModel):
    path: str


class DemoIn(BaseModel):
    reset: bool = False


@router.get("/api/projects")
def get_projects() -> dict[str, Any]:
    rows = []
    for r in read_recent():
        p = Path(r["path"])
        rows.append({**r, "exists": (p / CONFIG_FILENAME).is_file()})
    return {
        "recent": rows,
        "demo_available": demo_dir() is not None,
        "cwd": str(Path.cwd()),
        "home": str(Path.home()),
        "default_parent": str(default_parent()),
        "sep": os.sep,
    }


@router.post("/api/projects/open")
def post_open(body: PathIn, request: Request) -> dict[str, Any]:
    _local_only(request)
    project_dir = Path(body.path).expanduser()
    if project_dir.name == CONFIG_FILENAME:
        project_dir = project_dir.parent
    if not (project_dir / CONFIG_FILENAME).is_file():
        raise SetupError(
            404,
            f"no {CONFIG_FILENAME} in {project_dir}",
            "pick the folder that holds caliper.label.yaml, or start a new project",
        )
    try:
        cfg = load_project(project_dir)
    except ProjectFileError as exc:
        raise SetupError(422, str(exc), exc.hint) from None
    _open(request, project_dir.resolve())
    remember(project_dir, cfg.name)
    return {"project": _project_summary(project_dir.resolve(), cfg)}


@router.post("/api/projects/forget")
def post_forget(body: PathIn, request: Request) -> dict[str, Any]:
    _local_only(request)
    forget(body.path)
    return {"ok": True}


@router.post("/api/projects/demo")
def post_demo(request: Request, body: DemoIn | None = None) -> dict[str, Any]:
    _local_only(request)
    project_dir = prepare_demo(reset=bool(body and body.reset))
    cfg = load_project(project_dir)
    _open(request, project_dir)
    _snapshot(request)
    remember(project_dir, cfg.name)
    return {"project": _project_summary(project_dir, cfg)}


# --------------------------------------------------------------------------- wizard


@router.get("/api/setup/templates")
def get_templates() -> dict[str, Any]:
    return {"templates": [t.model_dump() for t in TEMPLATES]}


class TemplateIn(BaseModel):
    categories: str | None = None


@router.post("/api/setup/templates/{template_id}")
def post_template(template_id: str, body: TemplateIn | None = None) -> dict[str, Any]:
    try:
        labels = template_labels(template_id, body.categories if body else None)
    except ValueError as exc:
        raise SetupError(404, str(exc), "see GET /api/setup/templates") from None
    return {"labels": labels.model_dump(mode="json")}


@router.get("/api/setup/connectors")
def get_connectors() -> dict[str, Any]:
    from .connectors import available_connectors

    return {"connectors": available_connectors()}


@router.put("/api/setup/upload")
async def put_upload(request: Request, filename: str, draft: str | None = None) -> dict[str, Any]:
    _local_only(request)
    from .connectors import ConnectorError
    from .connectors.duck import save_upload

    d = _draft_dir(draft)
    if d is None:
        draft = secrets.token_hex(8)
        d = home() / "drafts" / draft
    data_dir = d / "data"
    with tempfile.SpooledTemporaryFile(max_size=8 << 20) as tmp:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise SetupError(
                    413,
                    "file too large to upload",
                    f"files up to {MAX_UPLOAD >> 20} MB; for bigger data choose 'Local file' "
                    "and give its path instead",
                )
            tmp.write(chunk)
        if size == 0:
            raise SetupError(400, "the uploaded file is empty", "choose a file with data in it")
        tmp.seek(0)
        try:
            saved = save_upload(data_dir, filename, tmp)
        except ConnectorError as exc:
            raise SetupError(400, exc.message, exc.hint) from None
    return {"draft": draft, "path": f"data/{saved.name}", "size": size, "name": saved.name}


class PreviewIn(BaseModel):
    source: dict[str, Any]
    draft: str | None = None
    project: bool = False  # resolve relative paths against the open project (settings)
    n: int = 50


def _source_base(request: Request, body: PreviewIn) -> Path:
    if body.project:
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is None:
            raise SetupError(409, "no project open", "open the project first")
        return Path(ctx.project_dir)
    d = _draft_dir(body.draft)
    return d if d is not None else Path.cwd()


@router.post("/api/setup/preview")
def post_preview(body: PreviewIn, request: Request) -> dict[str, Any]:
    """Credential status, columns (with detected display types), first rows and the count.

    A source that cannot be read is a normal answer here (`ok: false` plus `error`), so the
    wizard can show the fix next to the credential checks.
    """
    _local_only(request)  # an arbitrary source can read files / use the server's credentials
    from .connectors import ConnectorError, open_connector
    from .credentials import credential_status, redact

    try:
        source = SourceConfig.model_validate(body.source)
    except ValidationError as exc:
        raise SetupError(
            422, f"source: {_short_errors(exc)}", "check the connection form"
        ) from None
    base = _source_base(request, body)
    try:
        creds = [c.model_dump() for c in credential_status(source)]
    except ConnectorError as exc:
        creds = [{"name": "credentials", "kind": "none", "resolved": False, "hint": exc.hint}]
    out: dict[str, Any] = {"ok": False, "credentials": creds, "columns": [], "rows": []}
    out["count"] = None
    try:
        conn = open_connector(source, base)
        try:
            cols = conn.schema()
            rows = conn.preview(max(1, min(body.n, 200)))
            try:
                count = conn.count()
            except ConnectorError:
                count = None
        finally:
            with contextlib.suppress(Exception):
                conn.close()  # type: ignore[attr-defined]
    except ConnectorError as exc:
        out["error"] = {"detail": redact(exc.message, source), "hint": exc.hint}
        return out
    except Exception as exc:  # any driver error: never leak values
        out["error"] = {
            "detail": redact(f"could not read the source: {exc}", source),
            "hint": "check the path / table and the credentials above",
        }
        return out
    out.update(
        ok=True,
        columns=[c.model_dump(mode="json") for c in cols],
        rows=rows,
        count=count,
    )
    return out


class ConfigIn(BaseModel):
    config: dict[str, Any]


@router.post("/api/setup/validate")
def post_validate(body: ConfigIn) -> dict[str, Any]:
    cfg, issues = _parse_config(body.config)
    return {
        "issues": [i.model_dump() for i in issues],
        "ok": cfg is not None and not errors(issues),
        "yaml": render_project(cfg) if cfg else None,
    }


@router.get("/api/setup/browse")
def get_browse(request: Request, path: str | None = None, files: bool = True) -> dict[str, Any]:
    """Folders (and data files) under `path` for the folder/file picker. Local requests only."""
    _local_only(request)
    p = Path(path).expanduser() if path else Path.cwd()
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        raise SetupError(404, f"no such folder: {p}", "check the path, or start from your home")
    entries = []
    try:
        children = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
    except PermissionError:
        raise SetupError(403, f"cannot read {p}", "pick a folder you can open") from None
    for c in children:
        if c.name.startswith("."):
            continue
        try:
            if c.is_dir():
                kind = "project" if (c / CONFIG_FILENAME).is_file() else "dir"
                entries.append({"name": c.name, "path": str(c), "kind": kind})
            elif files and c.suffix.lower() in DATA_EXT:
                entries.append(
                    {"name": c.name, "path": str(c), "kind": "file", "size": c.stat().st_size}
                )
        except OSError:
            continue
        if len(entries) >= 500:
            break
    parent = str(p.parent) if p.parent != p else None
    return {
        "path": str(p.resolve()),
        "parent": parent,
        "is_project": (p / CONFIG_FILENAME).is_file(),
        "entries": entries,
    }


class DirIn(BaseModel):
    dir: str


@router.post("/api/setup/check-dir")
def post_check_dir(body: DirIn, request: Request) -> dict[str, Any]:
    """Warnings for the Review step's folder field. Never blocks: the user may want this."""
    _local_only(request)
    raw = body.dir.strip()
    p = Path(raw).expanduser() if raw else Path()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    root = git_root(p)
    has_project = (p / CONFIG_FILENAME).is_file()
    not_empty = p.is_dir() and any(c for c in p.iterdir() if not c.name.startswith("."))
    warnings: list[str] = []
    if has_project:
        warnings.append("This folder already holds a project. Choose another folder.")
    elif p.exists() and not p.is_dir():
        warnings.append("That path is a file. Choose a folder.")
    elif not_empty:
        warnings.append("This folder is not empty. The project files go next to what is there.")
    if root is not None:
        warnings.append(
            f"This folder is inside a git checkout ({root}). caliper.label.yaml would show up in "
            "its changes; the labels themselves stay ignored by git."
        )
    return {
        "path": str(p),
        "git_root": str(root) if root else None,
        "exists": p.exists(),
        "has_project": has_project,
        "warnings": warnings,
    }


class CreateIn(BaseModel):
    dir: str
    config: dict[str, Any]
    draft: str | None = None
    overwrite: bool = False
    open: bool = True


def _relocate_source(
    cfg: ProjectConfig, project_dir: Path, draft_dir: Path | None
) -> ProjectConfig:
    """Move staged uploads into <project>/data and make local paths project-relative."""
    src = cfg.source
    if src.kind not in {SourceKind.FILE, SourceKind.FOLDER, SourceKind.SQLITE} or not src.path:
        return cfg
    path = src.path
    if draft_dir is not None and not Path(path).is_absolute() and (draft_dir / path).exists():
        target = project_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise SetupError(
                409, f"{target} already exists", "choose an empty folder for the new project"
            )
        shutil.move(str(draft_dir / path), str(target))
        shutil.rmtree(draft_dir, ignore_errors=True)
        return cfg
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    try:
        new_path = str(p.resolve().relative_to(project_dir.resolve()))
    except ValueError:
        new_path = str(p.resolve())
    return cfg.model_copy(update={"source": src.model_copy(update={"path": new_path})})


def _snapshot(request: Request) -> dict[str, Any] | None:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return None
    from . import snapshot

    info = snapshot.take(ctx)
    return info.model_dump(mode="json") if hasattr(info, "model_dump") else None


@router.post("/api/setup/create")
def post_create(body: CreateIn, request: Request) -> dict[str, Any]:
    """Write the project config (and move uploads in), then open it and take the snapshot."""
    _local_only(request)
    cfg, issues = _parse_config(body.config)
    errs = errors(issues)
    if cfg is None or errs:
        first = errs[0] if errs else issues[0]
        raise SetupError(422, f"{first.path}: {first.message}", first.hint or "fix it and retry")
    project_dir = Path(body.dir).expanduser()
    if not project_dir.is_absolute():
        project_dir = Path.cwd() / project_dir
    project_dir = project_dir.resolve()
    if (project_dir / CONFIG_FILENAME).exists() and not body.overwrite:
        raise SetupError(
            409,
            f"{project_dir} already holds a project",
            "choose another folder, or open that project from the start screen",
        )
    if project_dir.exists() and not project_dir.is_dir():
        raise SetupError(409, f"{project_dir} is a file", "choose a folder")
    project_dir.mkdir(parents=True, exist_ok=True)
    cfg = _relocate_source(cfg, project_dir, _draft_dir(body.draft))
    path = save_project(project_dir, cfg)
    (project_dir / STATE_DIRNAME).mkdir(exist_ok=True)
    remember(project_dir, cfg.name)
    write_prefs(last_parent=str(project_dir.parent))
    out: dict[str, Any] = {
        "project": _project_summary(project_dir, cfg),
        "yaml": path.read_text(encoding="utf-8"),
        "snapshot": None,
    }
    if body.open:
        _open(request, project_dir)
        out["snapshot"] = _snapshot(request)
    return out
