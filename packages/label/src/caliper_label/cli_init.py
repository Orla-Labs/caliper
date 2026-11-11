"""`caliper label init`: create a project headlessly, from flags (dev 2).

    caliper label init triage --data tickets.csv --fields subject,body --labels correct_wrong_unsure
    caliper label init triage --kind postgres --table tickets --env DATABASE_URL --id-column id
    caliper label init demo --from-legacy label.yaml --data golden.jsonl

It writes `DIR/caliper.label.yaml` (the same file the wizard writes) and `.caliper-label/`.
Without `--fields`, the card shows the first columns the connector finds, with auto-detected
display types. `--no-detect` skips opening the source (CI, offline): then `--fields` is needed.

Dev 3 wires it in `cli.py` with one line: `cli_init.register(label_subparsers)`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2

_EXT_KIND = {".sqlite": "sqlite", ".sqlite3": "sqlite", ".db": "sqlite"}


def _infer_kind(data: str | None) -> str:
    if not data:
        return "file"
    low = data.lower()
    for prefix, kind in (
        ("s3://", "s3"),
        ("r2://", "s3"),
        ("gs://", "gcs"),
        ("gcs://", "gcs"),
        ("http://", "http"),
        ("https://", "http"),
        ("postgres://", "postgres"),
        ("postgresql://", "postgres"),
        ("mysql://", "mysql"),
    ):
        if low.startswith(prefix):
            return kind
    p = Path(data)
    if p.suffix.lower() in _EXT_KIND:
        return "sqlite"
    if p.is_dir():
        return "folder"
    return "file"


def _text_arg(v: str | None) -> str | None:
    """`@file` reads the file (markdown instructions, category lists)."""
    if v and v.startswith("@"):
        return Path(v[1:]).read_text(encoding="utf-8")
    return v


def _split(v: str | None) -> list[str]:
    if not v:
        return []
    parts = re.split(r"[,\n]", v)
    return [p.strip() for p in parts if p.strip()]


def build_config(args: argparse.Namespace, project_dir: Path) -> Any:
    from .project import (
        CardConfig,
        CardField,
        Credentials,
        DisplayType,
        GoldenCheck,
        LabelOption,
        LabelsConfig,
        LabelType,
        ProjectConfig,
        QueueConfig,
        ScoreRange,
        SourceConfig,
        WorkflowConfig,
    )
    from .project_setup import auto_colors, auto_hotkeys, load_legacy, slug, template_labels

    if args.from_legacy:
        if not args.data:
            raise ValueError("--from-legacy needs --data (the JSONL the old config was used with)")
        return load_legacy(args.from_legacy, args.data, project_dir=project_dir)

    kind = args.kind or _infer_kind(args.data)
    path = args.data
    if kind in ("postgres", "mysql") and path and "://" in path:
        raise ValueError(
            "do not put a connection URL on the command line (it ends up in shell history and "
            "the project file); export it, e.g. DATABASE_URL=..., and pass --env DATABASE_URL"
        )
    if path and kind in ("file", "folder", "sqlite") and not re.match(r"^[a-z0-9]+://", path):
        p = Path(path).expanduser()
        if args.copy_data and p.is_file():
            dest = project_dir / "data" / p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            path = f"data/{p.name}"
        else:
            try:
                path = str(p.resolve().relative_to(project_dir.resolve()))
            except ValueError:
                path = str(p.resolve())
    source = SourceConfig(
        kind=kind,
        path=path,
        table=args.table,
        query=_text_arg(args.query),
        id_column=args.id_column,
        format=args.format,
        credentials=Credentials(
            env=args.env,
            aws_profile=args.aws_profile,
            region=args.region,
            endpoint_url=args.endpoint_url,
        ),
    )

    columns: dict[str, DisplayType] = {}
    if not args.no_detect:
        from .connectors import open_connector

        conn = open_connector(source, project_dir)
        columns = {c.name: c.display for c in conn.schema()}
    names = _split(args.fields)
    if not names:
        if not columns:
            raise ValueError("--no-detect needs --fields (the columns the card shows)")
        skip = {args.id_column, args.golden_column, args.queue_by}
        names = [c for c in columns if c not in skip][:4]
    fields = []
    for name in names:
        col, _, typ = name.partition(":")  # "body:long_text" sets the display type
        dtype = DisplayType(typ) if typ else columns.get(col, DisplayType.TEXT)
        fields.append(CardField(column=col, type=dtype))

    if args.options:
        opts = auto_colors([LabelOption(value=slug(o), label=o) for o in _split(args.options)])
        labels = LabelsConfig(options=auto_hotkeys(opts))
        if args.approved:
            ok = set(_split(args.approved))
            labels.options = [
                o.model_copy(update={"approved": o.value in ok or o.label in ok})
                for o in labels.options
            ]
    elif args.labels == "free_text":
        labels = LabelsConfig(type=LabelType.FREE_TEXT)
    elif args.labels == "score":
        labels = LabelsConfig(type=LabelType.SCORE, score=ScoreRange())
    else:
        cats = args.categories or ""
        # @file: one category per line (commas allowed); inline: comma separated
        names = _text_arg(cats).splitlines() if cats.startswith("@") else _split(cats)
        labels = template_labels(args.labels, names)
    if args.multi:
        labels = labels.model_copy(update={"type": LabelType.MULTI})

    workflow = WorkflowConfig(
        queues=QueueConfig(by=args.queue_by),
        double_label=args.double_label,
        assignment=args.assignment,
        instructions=_text_arg(args.instructions),
        golden=GoldenCheck(answer_column=args.golden_column, rate=args.golden_rate)
        if args.golden_column
        else None,
    )
    return ProjectConfig(
        name=args.name or project_dir.resolve().name.replace("_", " ").replace("-", " ").title(),
        source=source,
        card=CardConfig(fields=fields),
        labels=labels,
        workflow=workflow,
    )


def cmd_init(args: argparse.Namespace) -> int:
    from .connectors import ConnectorError
    from .project import CONFIG_FILENAME, ProjectFileError, save_project
    from .project_setup import errors, validate_project

    project_dir = Path(args.dir).expanduser()
    if (project_dir / CONFIG_FILENAME).exists() and not args.force:
        print(
            f"caliper label init: {project_dir / CONFIG_FILENAME} exists "
            "(edit it, open it with `caliper label`, or pass --force)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    project_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg = build_config(args, project_dir)
    except ConnectorError as exc:
        print(f"caliper label init: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        return EXIT_USAGE
    except (ValueError, OSError, ProjectFileError) as exc:
        print(f"caliper label init: {exc}", file=sys.stderr)
        return EXIT_USAGE
    issues = validate_project(cfg)
    for i in issues:
        tag = "error" if i.severity == "error" else "warning"
        hint = f" ({i.hint})" if i.hint else ""
        print(f"  {tag}: {i.path}: {i.message}{hint}", file=sys.stderr)
    if errors(issues):
        print("caliper label init: nothing written", file=sys.stderr)
        return EXIT_USAGE
    path = save_project(project_dir, cfg)
    n_items = None
    if args.snapshot:
        from types import SimpleNamespace

        from . import snapshot
        from .connectors import open_connector
        from .project_store import ProjectStore

        store = ProjectStore.open(project_dir)
        try:
            ctx = SimpleNamespace(
                project_dir=project_dir,
                config=cfg,
                store=store,
                connector=open_connector(cfg.source, project_dir),
            )
            snapshot.take(ctx)
            n_items = store.count()
        finally:
            store.close()
    print(f"wrote {path}")
    print(
        f"  {len(cfg.card.fields)} card field(s), {len(cfg.labels.options)} option(s)"
        + (f", {n_items} item(s) snapshotted" if n_items is not None else "")
    )
    print(f"next: caliper label {project_dir}")
    return EXIT_OK


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("dir", nargs="?", default=".", help="project folder (default: here)")
    p.add_argument("--name", help="project name (default: from the folder name)")
    src = p.add_argument_group("data source (credentials are referenced, never stored)")
    src.add_argument("--data", help="file, folder, glob, s3:// gs:// or https:// URL, .sqlite")
    src.add_argument(
        "--kind",
        choices=["file", "folder", "s3", "gcs", "http", "postgres", "mysql", "sqlite"],
        help="source kind (default: inferred from --data)",
    )
    src.add_argument("--format", help="csv|tsv|jsonl|json|parquet|xlsx (default: by extension)")
    src.add_argument("--table", help="database table")
    src.add_argument("--query", help="SQL query instead of --table (@file.sql reads a file)")
    src.add_argument("--id-column", help="column holding a stable item id (default: row number)")
    src.add_argument("--env", help="env var with the DSN or token, e.g. DATABASE_URL")
    src.add_argument("--aws-profile")
    src.add_argument("--region")
    src.add_argument("--endpoint-url", help="S3-compatible endpoint (R2, MinIO)")
    src.add_argument(
        "--copy-data", action="store_true", help="copy a local file into DIR/data/ first"
    )
    src.add_argument(
        "--no-detect", action="store_true", help="do not open the source (needs --fields)"
    )
    card = p.add_argument_group("card")
    card.add_argument(
        "--fields",
        help="columns to show, comma separated; `col:type` sets the display type "
        "(text, long_text, number, badge, link, image, json). Default: first 4 columns",
    )
    lab = p.add_argument_group("labels")
    lab.add_argument(
        "--labels",
        default="correct_wrong_unsure",
        choices=[
            "correct_wrong_unsure",
            "true_uncertain_false",
            "yes_no_unsure",
            "categories",
            "rating_1_5",
            "free_text",
            "score",
        ],
        help="label template (default: correct_wrong_unsure)",
    )
    lab.add_argument(
        "--categories", help="for --labels categories: 'A,B,C' or @file (one per line)"
    )
    lab.add_argument("--options", help="your own options, comma separated (hotkeys 1..9)")
    lab.add_argument("--approved", help="with --options: which ones count as approved")
    lab.add_argument("--multi", action="store_true", help="allow several options per item")
    wf = p.add_argument_group("workflow")
    wf.add_argument("--queue-by", help="split queues by this column (e.g. stratum)")
    wf.add_argument("--double-label", type=int, default=1, help="labels per item (1-10)")
    wf.add_argument("--assignment", choices=["open", "round_robin"], default="open")
    wf.add_argument("--instructions", help="markdown, or @file.md")
    wf.add_argument("--golden-column", help="column with known answers (hidden golden checks)")
    wf.add_argument("--golden-rate", type=float, default=0.05)
    p.add_argument(
        "--from-legacy", metavar="LABEL_YAML", help="convert an old label.yaml (with --data)"
    )
    p.add_argument("--snapshot", action="store_true", help="also snapshot the items now")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")


def register(label_subparsers: Any) -> argparse.ArgumentParser:
    """Add `init` to the `caliper label` subparsers (called from dev 3's cli.py)."""
    p = label_subparsers.add_parser(
        "init",
        help="create a project (caliper.label.yaml) from flags, without the browser",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    add_arguments(p)
    p.set_defaults(func=cmd_init)
    return p
