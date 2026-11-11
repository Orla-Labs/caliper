"""`caliper label` subcommands, registered into the `caliper` CLI.

    caliper label [PROJECT_DIR]       no dir: start screen (new / open / demo); dir: that project
                  [--host 127.0.0.1] [--port 8765] [--auth token|none] [--no-browser]
    caliper label serve DATA.jsonl --config label.yaml [--labeller NAME]
                        [--store labels.jsonl|.sqlite] [common flags]       (legacy)
    caliper label export [PROJECT_DIR] --format F --out DEST [--conflicts exclude|majority]
    caliper label export --data DATA.jsonl --config label.yaml --out OUT.jsonl  (legacy)
    caliper label init ...            (dev 2's cli_init, when installed)

An optional positional next to subcommands is ambiguous for argparse, so `label`
takes the rest of the command line and dispatches itself: a known subcommand
name first means that subcommand, anything else opens a project.

Heavy imports (FastAPI, uvicorn) happen inside the handlers, so registering this
module costs nothing for other subcommands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
ALL_INTERFACES = ("0.0.0.0", "::")  # noqa: S104 - only compared, never bound here
DEFAULT_PORT = 8765


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def default_store(data: str) -> Path:
    """build/labels/<data stem>.labels.jsonl: outside the data folder, ignored by git."""
    return Path("build") / "labels" / f"{Path(data).stem}.labels.jsonl"


def _load(data: str, config_path: str) -> tuple[Any, Any]:
    from .config import load_label_config
    from .data import load_dataset

    config = load_label_config(config_path)
    return load_dataset(data, config), config


# --------------------------------------------------------------------------- serving


def _token(args: argparse.Namespace) -> str | None:
    if getattr(args, "auth", "none") != "token":
        return None
    from .auth import make_token

    return make_token()


def _run(app: Any, args: argparse.Namespace, token: str | None, path: str = "/") -> None:
    import uvicorn

    from .auth import install_log_redaction

    host_for_url = "127.0.0.1" if args.host in ALL_INTERFACES else args.host
    url = f"http://{host_for_url}:{args.port}{path}"
    if token:
        print(f"team mode: share this link (it carries the access token): {url}?token={token}")
        print("  scripts: send the header  Authorization: Bearer <token>")
    else:
        print(f"open {url}  (Ctrl+C to stop)")
    if args.host in ALL_INTERFACES and not token:
        _err("note: listening on all interfaces without a token; add --auth token for team mode")
    sys.stdout.flush()
    if not args.no_browser:
        import threading
        import webbrowser

        open_url = f"{url}?token={token}" if token else url
        threading.Timer(0.8, lambda: webbrowser.open(open_url)).start()
    install_log_redaction(token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


def cmd_serve(args: argparse.Namespace) -> int:
    from .state import Session
    from .store import open_store

    try:
        dataset, config = _load(args.data, args.config)
        store_path = Path(args.store) if args.store else default_store(args.data)
        store = open_store(store_path)
        session = Session(dataset, config, store)
    except (OSError, ValueError) as exc:
        _err(f"caliper label serve: {exc}")
        return EXIT_USAGE

    from .app import create_app

    token = _token(args)
    app = create_app(session, default_labeller=args.labeller, auth_token=token)
    n_done = sum(1 for it in dataset.items if session.is_complete(it.id))
    print(
        f"caliper label: {len(dataset.items)} items ({dataset.kind.value}), "
        f"{len(dataset.queues)} queue(s), {n_done} complete"
    )
    print(f"labels: {store_path}")
    if session.unknown_items:
        _err(f"note: {len(session.unknown_items)} labelled item id(s) are not in {args.data}")
    try:
        _run(app, args, token)
    finally:
        store.close()
    return EXIT_OK


def open_project(project_dir: str) -> Any:
    from .context import ProjectContext

    return ProjectContext.open(project_dir)


def cmd_open(args: argparse.Namespace) -> int:
    """`caliper label [PROJECT_DIR]`: the start screen, or straight into a project."""
    from .app import create_app

    ctx = None
    if args.project_dir:
        d = Path(args.project_dir)
        if not d.is_dir():
            _err(f"caliper label: {d} is not a folder")
            _err("  run `caliper label` with no folder to create a project from the start screen")
            return EXIT_USAGE
        try:
            ctx = open_project(str(d))
        except Exception as exc:  # one clear line, not a traceback
            hint = getattr(exc, "hint", None)
            _err(f"caliper label: cannot open {d}: {getattr(exc, 'detail', None) or exc}")
            if hint:
                _err(f"  fix: {hint}")
            return EXIT_USAGE
    token = _token(args)
    app = create_app(ctx=ctx, auth_token=token)
    if ctx is not None:
        s = ctx.session
        print(
            f"caliper label: {ctx.config.name}: {s.items.count(None)} items, "
            f"{len(s.items.queues())} queue(s), version {ctx.version}"
        )
        _run(app, args, token, "/")
    else:
        print("caliper label: start screen (new project / open project / try the demo)")
        _run(app, args, token, "/")
    return EXIT_OK


# --------------------------------------------------------------------------- export


def _print_summary(cons: Any, fmt: str, policy: str, needed: int, total: int | None) -> None:
    if fmt == "records":
        return
    of = f"of {total}; " if total is not None else ""
    print(
        f"  resolved {len(cons.resolved)}, conflicts {len(cons.conflicts)}, "
        f"incomplete {len(cons.incomplete)}, unlabelled {cons.unlabelled} "
        f"({of}double_label={needed}, conflicts={policy})"
    )
    for iid, labels in sorted(cons.conflicts.items()):
        who = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        _err(f"  left out (conflict): {iid}: {who}")
    for iid, k in sorted(cons.incomplete.items()):
        _err(f"  left out (incomplete, {k}/{needed} labels): {iid}")


def cmd_export(args: argparse.Namespace) -> int:
    if args.data or args.config:
        return _export_legacy(args)
    return _export_project(args)


def _export_legacy(args: argparse.Namespace) -> int:
    from .export import ConflictPolicy, ExportFormat, export
    from .store import load_records

    if not (args.data and args.config):
        _err("caliper label export: legacy export needs both --data and --config")
        return EXIT_USAGE
    if args.format not in ("golden", "rollouts", "records"):
        _err(
            f"caliper label export: --format {args.format} needs a project folder; "
            "legacy --data/--config exports golden, rollouts or records"
        )
        return EXIT_USAGE
    if not args.out:
        _err("caliper label export: --out is required")
        return EXIT_USAGE
    try:
        dataset, config = _load(args.data, args.config)
        store_path = Path(args.store) if args.store else default_store(args.data)
        if not store_path.exists():
            raise ValueError(f"no label store at {store_path}")
        records = load_records(store_path)
        n, cons = export(
            dataset,
            config,
            records,
            args.out,
            ExportFormat(args.format),
            ConflictPolicy(args.conflicts),
            include_unlabelled=args.include_unlabelled,
        )
    except (OSError, ValueError) as exc:
        _err(f"caliper label export: {exc}")
        return EXIT_USAGE

    print(f"wrote {n} {args.format} row(s) to {args.out}")
    _print_summary(cons, args.format, args.conflicts, config.double_label, len(dataset.items))
    return EXIT_OK


def _export_project(args: argparse.Namespace) -> int:
    from .export import (
        ConflictPolicy,
        ExportError,
        ExportFormat,
        RowsMode,
        is_remote,
        session_rows,
        write_local,
        write_via_sinks,
    )

    d = Path(args.project_dir or ".")
    if not (args.out or args.table):
        _err("caliper label export: give --out FILE|s3://... or --table NAME")
        return EXIT_USAGE
    try:
        ctx = open_project(str(d))
        fmt = ExportFormat(args.format)
        rows, cons = session_rows(
            ctx.session,
            fmt,
            ConflictPolicy(args.conflicts),
            rows=RowsMode(args.rows),
            include_unlabelled=args.include_unlabelled,
        )
        if args.table:
            src = ctx.config.source.model_dump(mode="json")
            dest = {
                "kind": src["kind"],
                "table": args.table,
                "credentials": src.get("credentials", {}),
            }
            if src.get("path") and src["kind"] == "sqlite":
                dest["path"] = src["path"]
            n = write_via_sinks(rows, dest, "table", project_dir=ctx.project_dir)
            where = f"table {args.table}"
        elif is_remote(args.out):
            kind = args.out.split("://", 1)[0]
            n = write_via_sinks(
                rows, {"kind": kind, "path": args.out}, fmt.value, overwrite=args.overwrite
            )
            where = args.out
        else:
            n = write_local(rows, fmt, args.out)
            where = args.out
    except ExportError as exc:
        _err(f"caliper label export: {exc}")
        if exc.hint:
            _err(f"  fix: {exc.hint}")
        return EXIT_USAGE
    except Exception as exc:  # one clear line, not a traceback
        _err(f"caliper label export: {getattr(exc, 'detail', None) or exc}")
        hint = getattr(exc, "hint", None)
        if hint:
            _err(f"  fix: {hint}")
        return EXIT_USAGE
    print(f"wrote {n} {args.format} row(s) to {where}")
    s = ctx.session
    _print_summary(cons, args.format, args.conflicts, s.needed, None)
    return EXIT_OK


# --------------------------------------------------------------------------- parsers


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to share on your network")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--auth",
        choices=["token", "none"],
        default="none",
        help="token: team mode, every request needs the access token "
        "(generated, or $CALIPER_LABEL_TOKEN)",
    )
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")


def build_parser() -> tuple[argparse.ArgumentParser, Any]:
    """The `caliper label` parser with its subcommands (exposed for cli_init.register)."""
    p = argparse.ArgumentParser(
        prog="caliper label",
        description="Label items fast in a local web UI. `caliper label` alone opens the "
        "start screen; `caliper label PROJECT_DIR` opens a project.",
    )
    sub = p.add_subparsers(dest="label_command", metavar="{serve,export,init}")

    s = sub.add_parser("serve", help="label a JSONL file with a label.yaml (legacy)")
    s.add_argument("data", help="GoldenRow, EvalRow or plain JSONL rows")
    s.add_argument("--config", required=True, help="label.yaml or label.toml")
    s.add_argument("--labeller", default=None, help="pre-fill the labeller name")
    s.add_argument(
        "--store",
        default=None,
        help="labels.jsonl (default build/labels/<data>.labels.jsonl) or labels.sqlite",
    )
    _common(s)
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser(
        "export", help="write labels: golden, rollouts, records, csv, jsonl, parquet, a table"
    )
    e.add_argument("project_dir", nargs="?", default=None, help="project folder (default .)")
    e.add_argument(
        "--format",
        choices=["golden", "rollouts", "records", "jsonl", "csv", "parquet", "table"],
        default="golden",
    )
    e.add_argument("--out", default=None, help="file path or s3://bucket/key")
    e.add_argument("--table", default=None, help="write back to this table in the source database")
    e.add_argument("--overwrite", action="store_true", help="s3: replace an existing object")
    e.add_argument(
        "--rows",
        choices=["items", "labels"],
        default="items",
        help="label table: one row per item (final label) or one per label",
    )
    e.add_argument(
        "--conflicts",
        choices=["exclude", "majority"],
        default="exclude",
        help="double-labelled items that disagree (and are not adjudicated): "
        "leave out (default) or strict majority",
    )
    e.add_argument(
        "--include-unlabelled",
        action="store_true",
        help="golden: also write rows without a resolved label, unchanged; "
        "tables: also write conflicts and incomplete items, marked by status",
    )
    legacy = e.add_argument_group("legacy (serve DATA --config CFG)")
    legacy.add_argument("--data", default=None)
    legacy.add_argument("--config", default=None)
    legacy.add_argument("--store", default=None, help="default build/labels/<data>.labels.jsonl")
    e.set_defaults(func=cmd_export)

    try:  # dev 2: `caliper label init`
        from .cli_init import register as register_init  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        register_init(sub)
    return p, sub


def open_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="caliper label",
        description="Open the labelling UI: the start screen, or PROJECT_DIR. "
        "Subcommands: serve (legacy), export, init. See `caliper label --help`.",
    )
    p.add_argument("project_dir", nargs="?", default=None, help="a project folder")
    _common(p)
    p.set_defaults(func=cmd_open)
    return p


# Third-party modules the label UI needs; they ship in the `label` extra.
_LABEL_DEPS = {"fastapi", "starlette", "uvicorn", "yaml", "ruamel", "duckdb", "boto3", "pytz"}


def _call(ns: argparse.Namespace) -> int:
    """Run a label subcommand; a missing `label` extra becomes an install hint, not a traceback."""
    try:
        return int(ns.func(ns))
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in _LABEL_DEPS:
            raise
        _err(
            f"caliper label needs the label extra (missing module {exc.name!r}): "
            "pip install 'orlalabs-caliper[label]'  or  uv add 'orlalabs-caliper[label]'"
        )
        return EXIT_USAGE


def cmd_label(args: argparse.Namespace) -> int:
    argv: list[str] = list(args.label_argv or [])
    inner, sub = build_parser()
    if argv and argv[0] in sub.choices:
        ns = inner.parse_args(argv)
        return _call(ns)
    if argv and argv[0] in ("-h", "--help"):
        inner.print_help()
        print()
        print(open_parser().format_usage().strip())
        return EXIT_OK
    ns = open_parser().parse_args(argv)
    return _call(ns)


def register(subparsers: Any) -> None:
    """Add `label` to the `caliper` subparsers."""
    lab = subparsers.add_parser(
        "label",
        help="label items fast in a local web UI (start screen, projects, team mode)",
        add_help=False,
        prefix_chars="\x00",  # every argument goes to cmd_label's own parsers
    )
    lab.add_argument("label_argv", nargs=argparse.REMAINDER)
    lab.set_defaults(func=cmd_label)
