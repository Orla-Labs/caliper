"""FastAPI app factory for `caliper label`: JSON API + the no-build ES-module UI.

Two ways in:

* ``create_app(ctx=ProjectContext | None)``: a project (or, with ``None``, the start
  screen). Routes that need a project answer 409 until one is opened with
  :func:`caliper_label.context.set_ctx`.
* ``create_app(session)``: legacy ``serve DATA --config CFG`` (a
  :class:`~caliper_label.state.Session` over the JSONL file). Same UI, same API.

Routers are included with one line each. Dev 1's ``routes_data`` and dev 2's
``routes_setup`` / ``routes_settings`` are picked up automatically when present.
The ``/api`` catch-all 404 is registered *after* every router; keep it last.

Local images are served only from the image root (the data file's directory by
default). The client never sends a path: it asks for ``/api/image/{item}/{n}``
(legacy) or ``/api/media/{item}/{field}`` and the server resolves that field of
that item itself, then refuses anything outside the root or not an image type.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import routes_export, routes_label, routes_team
from .auth import TokenAuthMiddleware
from .errors import body, install_handlers
from .routes_label import IMAGE_TYPES, resolve_image  # noqa: F401  (re-exported)
from .state import Session

STATIC_DIR = Path(__file__).parent / "static"

OPTIONAL_ROUTERS = ("routes_data", "routes_setup", "routes_settings")  # dev 1, dev 2

TAGS = [
    {"name": "items", "description": "The snapshot of items to label (paginated)."},
    {"name": "labels", "description": "Record, fetch and undo labels."},
    {"name": "labelling", "description": "What the labelling screen uses: config, queues, next."},
    {"name": "team", "description": "Dashboard, agreement (kappa) and adjudication."},
    {"name": "export", "description": "Export labels to files, a database table or S3."},
    {"name": "meta", "description": "Server status."},
]

DESCRIPTION = """\
Script `caliper label`: list items, post labels, fetch labels.

In team mode (`--auth token`) send `Authorization: Bearer <token>` with every request
(or `?token=<token>` once from a browser). Errors are always
`{"detail": "...", "hint": "..."}`, where `hint` says how to fix it.
"""


def create_app(
    session: Session | None = None,
    *,
    ctx: Any = None,
    default_labeller: str | None = None,
    image_root: Path | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="caliper label",
        description=DESCRIPTION,
        version="1",
        openapi_tags=TAGS,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.session = session
    app.state.ctx = ctx
    app.state.default_labeller = default_labeller
    app.state.auth_token = auth_token or None
    if session is not None and session.dataset is not None:
        config, dataset = session.config, session.dataset
        app.state.image_root = image_root or (
            (dataset.path.parent / config.image_root) if config.image_root else dataset.path.parent
        )
    else:
        app.state.image_root = image_root
    install_handlers(app)
    if auth_token:
        app.add_middleware(TokenAuthMiddleware, token=auth_token)

    app.include_router(routes_label.router)
    app.include_router(routes_team.router)
    app.include_router(routes_export.router)
    for name in OPTIONAL_ROUTERS:
        try:
            mod = importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.{name}":
                raise
            continue
        app.include_router(mod.router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # Keep this last: it would shadow any /api route registered after it.
    @app.api_route(
        "/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False
    )
    def api_404(rest: str) -> JSONResponse:
        return JSONResponse(body("not found", "see the API docs at /docs"), status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
