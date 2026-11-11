"""`caliper kappa`: agreement numbers from the command line.

    caliper kappa --labels LABELS.jsonl [--by category] [--source human|all|SRC,...]
                  [--method auto|cohen|fleiss] [--json]
        Inter-rater agreement between labellers, from LabelRecord JSONL
        (what `caliper label` writes). Undo/skip/latest-per-labeller rules are
        applied first (see caliper_eval.inter_rater).

    caliper kappa --rollouts LABELED.jsonl --grader FILE.py:ATTR
                  [--weighting quadratic|linear|unweighted] [--bins 0,0.5,1] [--json]
        Grader vs human labels: Cohen's kappa between the grader's score and
        ``rollout.metadata.human_label`` on labeled-rollout JSONL (EvalRow
        lines). Rows without ``human_label`` count as unlabelled.

stdout is deterministic (no absolute paths or timestamps). Exit 0 ok, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _kappa_text(kappa: float, undefined: bool, reason: str | None) -> str:
    if undefined:
        return f"kappa undefined ({reason})" if reason else "kappa undefined"
    return f"kappa {kappa:+.3f}"


# --------------------------------------------------------------------------- labels


def _sources(value: str) -> Any:
    from caliper_eval.types import LabelSource

    if value == "human":
        return None
    if value == "all":
        return list(LabelSource)
    return [LabelSource(v.strip()) for v in value.split(",") if v.strip()]


def _cmd_labels(args: argparse.Namespace) -> int:
    from caliper_eval.inter_rater import (
        inter_rater_from_records,
        load_label_records,
        resolve_label_records,
    )

    try:
        sources = _sources(args.source)
        records = load_label_records(args.labels)
        kept = resolve_label_records(records, sources=sources)
        report = inter_rater_from_records(records, sources=sources, method=args.method)
    except (OSError, ValueError) as exc:
        _err(f"caliper kappa: {exc}")
        return EXIT_USAGE

    if args.json:
        print(report.model_dump_json(indent=2))
        return EXIT_OK

    o = report.overall
    print("Caliper kappa: inter-rater agreement")
    print(
        f"labels: {args.labels} ({len(records)} records, {len(kept)} kept after "
        f"undo/skip/latest; sources: {args.source})"
    )
    print(f"method: {report.method} ({o.n_raters} raters: {', '.join(o.raters) or '-'})")
    print(
        f"items:  {report.n_items_total} total, {o.n_items} rated by 2+ raters, "
        f"{report.n_items_single_rated} single-rated (excluded)"
    )
    print("")
    rows: list[tuple[str, Any]] = [("overall", o)]
    if args.by == "category":
        rows += list(report.by_category.items())
    width = max(len(name) for name, _ in rows)
    if args.by == "category":
        print("by category")
    for name, s in rows:
        print(
            f"  {name:<{width}}  n={s.n_items:<4} agree {_pct(s.percent_all_agree):>6}  "
            f"{_kappa_text(s.kappa, s.kappa_undefined, s.undefined_reason)}"
        )
    if args.by == "category" and not report.by_category:
        print("  (no record has a category)")
    if len(report.pairs) > 1:
        print("")
        print("pairs (Cohen's kappa on shared items)")
        for p in report.pairs:
            print(
                f"  {p.rater_a} vs {p.rater_b}  n={p.n_items:<4} "
                f"agree {_pct(p.observed_agreement):>6}  "
                f"{_kappa_text(p.kappa, p.kappa_undefined, p.undefined_reason)}"
            )
    if report.category_conflicts:
        _err(f"note: {len(report.category_conflicts)} item(s) have conflicting categories; "
             "the first category seen was used")
    return EXIT_OK


# --------------------------------------------------------------------------- rollouts


def _load_rollouts(path: str) -> list[Any]:
    from caliper_sdk import EvalRow

    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rows.append(EvalRow.model_validate(json.loads(line)))
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not an EvalRow: {exc}") from exc
    return rows


def _human_label(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"human_label must be a number or bool, got {value!r}")


def _cmd_rollouts(args: argparse.Namespace) -> int:
    from caliper_kappa import KappaWeighting, LabeledRow, LabelSet, compute_kappa

    from caliper_eval.loader import load_callable

    try:
        rows = _load_rollouts(args.rollouts)
        grader = load_callable(args.grader)
        if not callable(grader):
            raise ValueError(f"{args.grader} is not callable")
        bins = [float(b) for b in args.bins.split(",")] if args.bins else None
        labels = []
        for r in rows:
            score = _human_label(r.rollout.metadata.get("human_label"))
            if score is not None:
                labels.append(LabeledRow(row_id=r.id, label_score=score))
        report = compute_kappa(
            grader,
            rows,
            LabelSet(name="human_label", rows=labels),
            bin_edges=bins,
            weighting=KappaWeighting(args.weighting),
        )
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        _err(f"caliper kappa: {exc}")
        return EXIT_USAGE

    if args.json:
        print(report.model_dump_json(indent=2))
        return EXIT_OK

    name = getattr(grader, "name", args.grader)
    version = getattr(grader, "version", None)
    print("Caliper kappa: grader vs human labels")
    print(
        f"rollouts: {args.rollouts} ({report.n_eval_rows} rows, {report.n_labeled} with "
        f"human_label, {report.n_unlabeled} without)"
    )
    print(f"grader:   {name}" + (f" v{version}" if version else ""))
    print(f"weighting: {report.weighting.value}, bins {report.bin_edges}")
    print("")
    print(
        f"  n={report.n_scored}  observed {_pct(report.observed_agreement)}  "
        f"expected {_pct(report.expected_agreement)}  "
        f"{_kappa_text(report.kappa, report.kappa_undefined, None)}"
    )
    print("  confusion (rows = grader bucket, cols = label bucket)")
    for row in report.confusion:
        print("    " + " ".join(f"{c:>5}" for c in row))
    if report.failures:
        print(f"  failures: {len(report.failures)}")
        for f in report.failures:
            print(f"    {f.row_id}: {f.error_type}: {f.error_message}")
    return EXIT_OK


# --------------------------------------------------------------------------- wiring


def cmd_kappa(args: argparse.Namespace) -> int:
    if args.rollouts is not None:
        if not args.grader:
            _err("caliper kappa: --rollouts needs --grader FILE.py:ATTR")
            return EXIT_USAGE
        return _cmd_rollouts(args)
    if args.grader:
        _err("caliper kappa: --grader only applies with --rollouts")
        return EXIT_USAGE
    return _cmd_labels(args)


def register(subparsers: Any) -> None:
    """Add the ``kappa`` subcommand to ``caliper``'s subparsers."""
    k = subparsers.add_parser(
        "kappa",
        help="inter-rater agreement (--labels) or grader vs human labels (--rollouts)",
    )
    src = k.add_mutually_exclusive_group(required=True)
    src.add_argument("--labels", help="LabelRecord JSONL (e.g. from caliper label)")
    src.add_argument("--rollouts", help="labeled rollouts JSONL (metadata.human_label)")
    k.add_argument("--grader", default=None, help="FILE.py:ATTR or module:attr (--rollouts)")
    k.add_argument("--by", choices=["category"], default=None, help="break down by category")
    k.add_argument(
        "--source",
        default="human",
        help="label sources: human (default), all, or a comma list "
        "(human_end_user,internal_reviewer,llm_teacher,heuristic)",
    )
    k.add_argument("--method", choices=["auto", "cohen", "fleiss"], default="auto")
    k.add_argument(
        "--weighting",
        choices=["quadratic", "linear", "unweighted"],
        default="quadratic",
        help="grader-vs-label weighting (--rollouts)",
    )
    k.add_argument("--bins", default=None, help="bin edges, e.g. 0,0.5,1 (--rollouts)")
    k.add_argument("--json", action="store_true", help="print the report as JSON")
    k.set_defaults(func=cmd_kappa)


__all__ = ["cmd_kappa", "register"]
