"""`caliper label` subcommands, registered into the `caliper` CLI.

    caliper label serve DATA.jsonl --config label.yaml [--port 8765] [--labeller NAME]
                        [--store labels.jsonl|.sqlite] [--no-browser]
    caliper label export --data DATA.jsonl --config label.yaml --store labels.jsonl
                         --out OUT.jsonl [--format golden|rollouts|records]

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

    import uvicorn

    from .app import create_app

    app = create_app(session, default_labeller=args.labeller)
    url = f"http://{args.host}:{args.port}/"
    n_done = sum(1 for it in dataset.items if session.is_complete(it.id))
    print(
        f"caliper label: {len(dataset.items)} items ({dataset.kind.value}), "
        f"{len(dataset.queues)} queue(s), {n_done} complete"
    )
    print(f"labels: {store_path}")
    print(f"open {url}  (Ctrl+C to stop)")
    if session.unknown_items:
        _err(f"note: {len(session.unknown_items)} labelled item id(s) are not in {args.data}")
    sys.stdout.flush()
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        store.close()
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    from .export import ConflictPolicy, ExportFormat, export
    from .store import load_records

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
    if args.format != "records":
        print(
            f"  resolved {len(cons.resolved)}, conflicts {len(cons.conflicts)}, "
            f"incomplete {len(cons.incomplete)}, unlabelled {cons.unlabelled} "
            f"(of {len(dataset.items)}; double_label={config.double_label}, "
            f"conflicts={args.conflicts})"
        )
        for iid, labels in sorted(cons.conflicts.items()):
            who = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
            _err(f"  left out (conflict): {iid}: {who}")
        for iid, k in sorted(cons.incomplete.items()):
            _err(f"  left out (incomplete, {k}/{config.double_label} labels): {iid}")
    return EXIT_OK


def register(subparsers: Any) -> None:
    """Add `label` (with `serve` and `export`) to the `caliper` subparsers."""
    lab = subparsers.add_parser("label", help="label items fast in a local web UI")
    sub = lab.add_subparsers(dest="label_command", required=True)

    s = sub.add_parser("serve", help="start the labelling UI")
    s.add_argument("data", help="GoldenRow, EvalRow or plain JSONL rows")
    s.add_argument("--config", required=True, help="label.yaml or label.toml")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--labeller", default=None, help="pre-fill the labeller name")
    s.add_argument(
        "--store",
        default=None,
        help="labels.jsonl (default build/labels/<data>.labels.jsonl) or labels.sqlite",
    )
    s.add_argument("--no-browser", action="store_true", help="do not open a browser")
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser("export", help="write labels as golden rows, rollouts or raw records")
    e.add_argument("--data", required=True)
    e.add_argument("--config", required=True)
    e.add_argument("--store", default=None, help="default build/labels/<data>.labels.jsonl")
    e.add_argument("--out", required=True)
    e.add_argument("--format", choices=["golden", "rollouts", "records"], default="golden")
    e.add_argument(
        "--conflicts",
        choices=["exclude", "majority"],
        default="exclude",
        help="double-labelled items that disagree: leave out (default) or strict majority",
    )
    e.add_argument(
        "--include-unlabelled",
        action="store_true",
        help="golden: also write rows without a resolved label, unchanged",
    )
    e.set_defaults(func=cmd_export)
