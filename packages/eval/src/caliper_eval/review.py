"""`caliper review`: the outcomes a person must judge to say which candidate is better.

    caliper review --config caliper.toml --eval-out build/eval --out build/review

Reads an eval's ``results.jsonl`` and writes one review item per
``(row, outcome)`` that has no human verdict yet:

* both outcomes of every row where a candidate and the reference decide
  differently (only these rows can make one better than the other);
* the reference's outcome on the base sample (``[quality] base_sample`` rows
  drawn at random with the config's seed: the same rows ``caliper eval`` uses to
  measure how often the reference is wrong, which "X% fewer errors" needs).

Each item shows the request and one outcome, never the model that produced it,
and the items are shuffled. An item whose row has no other item (a base-sample
row, or a changed row whose other outcome was judged before) can hint at which
side produced it, never at which model.

With the ``label`` extra installed, the folder is also a ``caliper label``
project (Yes / No / Unsure); its ``--format jsonl`` export is an approvals file
for ``[quality] approvals``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ._stats import derive_seed
from .approvals import Verdicts, build_verdicts, read_approvals
from .noise import index_results
from .quality import base_sample_rows
from .types import GoldenRow, RowResult, request_sha256

EXIT_OK = 0
EXIT_USAGE = 2
ITEMS_FILE = "items.jsonl"
DEFAULT_BASE_SAMPLE = 30  # [quality] base_sample when the config has no [quality]
# Modules of the `label` extra that `caliper label` needs to open the project.
LABEL_EXTRA_MODULES = ("fastapi", "uvicorn", "yaml", "ruamel.yaml", "duckdb")
INSTRUCTIONS = (
    "Would you accept this outcome for this request, if the feature produced it?\n\n"
    "Judge the outcome only. You are not told which model produced it, and the items "
    "are shuffled. Use Unsure when you cannot tell; it counts as no verdict."
)


class ReviewItem(BaseModel):
    """One outcome on one golden row, for a person to approve or reject."""

    model_config = ConfigDict(extra="forbid")

    id: str  # hash of (row_id, outcome); reveals nothing about the model
    row_id: str  # golden row id
    outcome: str  # the outcome to judge
    request_sha256: str  # the request shown; a later change makes the verdict stale
    request: dict[str, Any]  # the golden row's request, as the feature sent it


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReviewItem]
    changed_rows: dict[str, int]  # candidate -> rows where it and the reference differ
    from_changes: int  # items from changed rows
    from_base_sample: int  # items from the base sample (not already in from_changes)
    already_judged: int  # outcomes skipped because a person has already judged them


def _item_id(row_id: str, outcome: str) -> str:
    return hashlib.sha256(f"{row_id}\0{outcome}".encode()).hexdigest()[:16]


def plan_review(
    golden: Sequence[GoldenRow],
    results: Sequence[RowResult],
    verdicts: Verdicts,
    reference: str,
    candidates: Sequence[str],
    *,
    base_rows: Sequence[str] = (),
    seed: int = 0,
) -> ReviewPlan:
    """The outcomes still needing a human verdict, shuffled (pure, deterministic)."""
    by_id = {g.id: g for g in golden}
    idx = index_results(results)
    ref_out: dict[str, str] = {}
    for (cand, row_id, replica), r in idx.items():
        if cand == reference and replica == 0 and r.decision is not None and row_id in by_id:
            ref_out[row_id] = r.decision.outcome

    wanted: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    judged: set[tuple[str, str]] = set()

    def want(row_id: str, outcome: str) -> bool:
        key = (row_id, outcome)
        if key in seen or key in judged:
            return False
        if verdicts.get(row_id, outcome) is not None:
            judged.add(key)
            return False
        seen.add(key)
        wanted.append(key)
        return True

    changed_rows: dict[str, int] = {}
    for cand in candidates:
        n = 0
        for row_id in sorted(ref_out):
            r = idx.get((cand, row_id, 0))
            if r is None or (r.decision is not None and r.decision.outcome == ref_out[row_id]):
                continue
            n += 1  # no decision is a change too, and needs no verdict of its own
            want(row_id, ref_out[row_id])
            if r.decision is not None:
                want(row_id, r.decision.outcome)
        changed_rows[cand] = n
    from_changes = len(wanted)

    for row_id in base_rows:
        if row_id in ref_out:
            want(row_id, ref_out[row_id])
    rng = random.Random(derive_seed(seed, "review"))  # noqa: S311 - shuffling, not crypto

    rng.shuffle(wanted)
    items = [
        ReviewItem(
            id=_item_id(row_id, outcome),
            row_id=row_id,
            outcome=outcome,
            request_sha256=request_sha256(by_id[row_id].request),
            request=by_id[row_id].request,
        )
        for row_id, outcome in wanted
    ]
    return ReviewPlan(
        items=items,
        changed_rows=changed_rows,
        from_changes=from_changes,
        from_base_sample=len(wanted) - from_changes,
        already_judged=len(judged),
    )


def _label_project(out: Path) -> Path | None:
    """Make ``out`` a `caliper label` project over the items; ``None`` without the extra.

    The ``caliper_label`` package ships in every install; its third-party modules come
    with the ``label`` extra, so those are what decide.
    """
    if any(importlib.util.find_spec(m) is None for m in LABEL_EXTRA_MODULES):
        return None
    from caliper_label import cli_init
    from caliper_label.project import CardField, save_project

    parser = argparse.ArgumentParser()
    cli_init.add_arguments(parser)
    args = parser.parse_args(
        [
            str(out),
            "--name",
            "Caliper review",
            "--data",
            str(out / ITEMS_FILE),
            "--id-column",
            "id",
            "--fields",
            "outcome:badge,request:json",
            "--labels",
            "yes_no_unsure",
            "--instructions",
            INSTRUCTIONS,
            "--no-detect",
        ]
    )
    config = cli_init.build_config(args, out)
    # Kept in the snapshot and the export (the approvals key), never shown on the card.
    hidden = [CardField(column=c, hidden=True) for c in ("row_id", "request_sha256")]
    config.card.fields = [*config.card.fields, *hidden]
    return save_project(out, config)


def cmd_review(args: argparse.Namespace) -> int:
    from .config import load_config
    from .loader import load_golden
    from .results import ResultsStore

    def err(msg: str) -> None:
        print(msg, file=sys.stderr)

    out = Path(args.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        err(f"caliper review: {out} is not an empty folder (use a new folder per review round)")
        return EXIT_USAGE
    try:
        config = load_config(args.config)
        golden = load_golden(config.golden_path)
        approvals = []
        golden_labels = True
        base_sample = DEFAULT_BASE_SAMPLE
        if config.quality is not None:
            golden_labels = config.quality.golden_labels
            base_sample = config.quality.base_sample
            for path in config.quality.approvals:
                approvals += read_approvals(config.resolve(path))
    except (OSError, ValueError) as exc:
        err(f"caliper review: {exc}")
        return EXIT_USAGE
    results_path = Path(args.eval_out) / "results.jsonl"
    if not results_path.is_file():
        err(f"caliper review: no {results_path}; run caliper eval --out {args.eval_out} first")
        return EXIT_USAGE
    results = ResultsStore(results_path).load()
    reference = config.reference or config.candidates[0].name
    names = [c.name for c in config.candidates if c.name != reference]
    if args.candidates:
        chosen = [n.strip() for n in args.candidates.split(",") if n.strip()]
        unknown = sorted(set(chosen) - set(names))
        if unknown:
            err(f"caliper review: unknown or reference candidates {unknown}; choose from {names}")
            return EXIT_USAGE
        names = chosen
    verdicts, _ = build_verdicts(
        golden,
        approvals,
        golden_labels=golden_labels,
        outcomes={r.decision.outcome for r in results if r.decision is not None},
    )
    plan = plan_review(
        golden,
        results,
        verdicts,
        reference,
        names,
        base_rows=base_sample_rows(golden, base_sample, config.seed),
        seed=config.seed,
    )

    out.mkdir(parents=True, exist_ok=True)
    with (out / ITEMS_FILE).open("w", encoding="utf-8") as fh:
        for item in plan.items:
            fh.write(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n")
    project = _label_project(out)

    changed = ", ".join(f"{k} {v}" for k, v in plan.changed_rows.items()) or "none"
    print(
        f"{len(plan.items)} outcomes to review: {plan.from_changes} from rows where a "
        f"candidate and {reference} decide differently ({changed}), "
        f"{plan.from_base_sample} from the base sample of {base_sample} rows; "
        f"{plan.already_judged} already judged"
    )
    print(f"wrote {out / ITEMS_FILE}")
    approvals_path = out / "approvals.jsonl"
    if project is not None:
        print(f"wrote {project}")
        print(f"next: caliper label {out}")
        print(f"then: caliper label export {out} --format jsonl --out {approvals_path}")
    else:
        print(
            "the review UI needs the label extra: pip install 'orlalabs-caliper[label]' "
            "(or record each person's verdict as one JSON line per item: row_id, outcome, "
            "request_sha256, approved, source)"
        )
    rel = os.path.relpath(approvals_path.resolve(), Path(config.config_dir).resolve())
    print(f'then add it to {args.config}: [quality] approvals = ["{Path(rel).as_posix()}"]')
    return EXIT_OK


def register(sub: Any) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "review",
        help="list the outcomes a person must approve to measure which candidate is better",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    p.add_argument("--config", required=True, help="eval TOML file")
    p.add_argument("--eval-out", required=True, help="the caliper eval --out folder to read")
    p.add_argument("--out", required=True, help="folder for the review items (a new one)")
    p.add_argument(
        "--candidates", default=None, help="comma-separated candidates (default: all but ref)"
    )
    p.set_defaults(func=cmd_review)
    return p


__all__ = ["ReviewItem", "ReviewPlan", "cmd_review", "plan_review", "register"]
