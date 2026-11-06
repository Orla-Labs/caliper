"""`caliper` command line.

    caliper eval   --config eval.toml [--out caliper-out] [--ci] [--summary-md PATH] ...
    caliper sample --in logs.jsonl --out golden.jsonl --stratum-field outcome \\
                   --keep positive,unsure --negatives 200

Exit codes: 0 ok, 1 CI gate failed (``--ci``), 2 usage/config error,
3 preflight failed, 4 budget exceeded (results kept; re-run to resume).

stdout is deterministic (no absolute paths, timestamps or run ids); progress
and notices go to stderr. Paths are printed exactly as the user passed them.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

EXIT_OK = 0
EXIT_GATE = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_BUDGET = 4


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _extra(section: Any, key: str) -> Any:
    """Optional knob from an extensible config section (``[ci]``, ``[calibration]``)."""
    extra = getattr(section, "model_extra", None) or {}
    return extra.get(key, getattr(section, key, None))


# --------------------------------------------------------------------------- eval


def _select_candidates(config: Any, names: list[str]) -> Any:
    if not names:
        return config
    known = [c.name for c in config.candidates]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown candidate(s) {unknown}; config has {known}")
    keep = set(names)
    if config.reference not in keep:
        _err(
            f"note: added reference '{config.reference}' to --candidates "
            "(needed for the noise floor and transitions)"
        )
        keep.add(config.reference)
    chosen = [c for c in config.candidates if c.name in keep]
    return config.model_copy(update={"candidates": chosen})


def cmd_eval(args: argparse.Namespace) -> int:
    from caliper_eval.budget import Budget, BudgetExceeded
    from caliper_eval.ci import evaluate_gate
    from caliper_eval.config import load_config
    from caliper_eval.loader import load_golden
    from caliper_eval.preflight import format_failures, run_preflight
    from caliper_eval.pricing import pricing_warnings
    from caliper_eval.report import build_report, render_markdown, render_text
    from caliper_eval.runner import run_eval

    try:
        config = load_config(args.config)
        config = _select_candidates(config, _csv(args.candidates))
        golden = load_golden(config.golden_path)
        decide = config.load_decide()
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        _err(f"caliper eval: {exc}")
        return EXIT_USAGE

    if not args.skip_preflight:
        checks = run_preflight(config.candidates, config)
        failed = [r for r in checks if not r.ok]
        if failed:
            _err(format_failures(failed))
            _err("caliper eval: preflight failed; fix the candidate or pass --skip-preflight")
            return EXIT_PREFLIGHT
        _err(f"preflight ok: {', '.join(r.candidate for r in checks)}")

    os.makedirs(args.out, exist_ok=True)
    results_path = os.path.join(args.out, "results.jsonl")
    budget_usd = args.budget_usd if args.budget_usd is not None else config.budget_usd

    def progress(result: Any, done: int, total: int) -> None:
        if done == total or done % max(1, total // 10) == 0:
            _err(f"  {done}/{total} executions")

    try:
        results = run_eval(
            config,
            results_path=results_path,
            budget=Budget(budget_usd),
            resume=not args.no_resume,
            decide=decide,
            progress=progress,
            retry_errors=args.retry_errors,
            rows=golden,
        )
    except BudgetExceeded as exc:
        _err(f"caliper eval: {exc}")
        _err(
            f"results so far: {results_path}. Resume with the same command "
            "(optionally with a higher --budget-usd)."
        )
        return EXIT_BUDGET
    except (ImportError, ValueError) as exc:  # e.g. provider SDK extra missing
        _err(f"caliper eval: cannot run candidates: {exc}")
        return EXIT_PREFLIGHT

    target = args.target_precision
    if target is None:
        target = _extra(config.calibration, "target_precision")
    report = build_report(
        config,
        results,
        golden,
        n_boot=args.n_boot,
        target_precision=None if target is None else float(target),
    )
    for name, warning in sorted(pricing_warnings(config.candidates, config.pricing).items()):
        report.warnings.append(f"pricing {name}: {warning}")

    retouched = _record_test_touches(args, config, golden)

    gate = None
    if args.ci:
        max_err = args.max_error_rate
        if max_err is None:
            max_err = _extra(config.ci, "max_error_rate") or 0.0
        gate = evaluate_gate(
            report,
            max_error_rate=float(max_err),
            retouched=None if args.allow_test_retouch else retouched,
        )

    print(render_text(report, gate), end="")
    print(f"results: {results_path}")
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as fh:
        fh.write(report.model_dump_json(indent=2) + "\n")
    if args.summary_md:
        md_dir = os.path.dirname(args.summary_md)
        if md_dir:
            os.makedirs(md_dir, exist_ok=True)
        with open(args.summary_md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report, gate))
        print(f"summary: {args.summary_md}")
    if gate is not None and not gate.passed:
        return EXIT_GATE
    return EXIT_OK


def _record_test_touches(args: argparse.Namespace, config: Any, golden: Any) -> dict[str, int]:
    """Append this run's test-split touches to the ledger; warn on stderr about re-touches."""
    from caliper_eval.ledger import record_touches

    path = args.test_ledger or os.path.join(args.out, "test_ledger.jsonl")
    try:
        retouches = record_touches(path, config.candidates, golden, config.reference)
    except (OSError, ValueError) as exc:
        _err(f"warning: could not update the test ledger {path}: {exc}")
        return {}
    for r in retouches:
        verdict = (
            "accepted (--allow-test-retouch)"
            if args.allow_test_retouch
            else ("fails the gate under --ci" if args.ci else "warning")
        )
        _err(
            f"test split re-touched: {r.candidate} was already reported on this test set "
            f"{r.previous_touches}x with the same settings ({verdict}; ledger {path})"
        )
    return {r.candidate: r.previous_touches for r in retouches}


# --------------------------------------------------------------------------- sample


def cmd_sample(args: argparse.Namespace) -> int:
    from caliper_eval.sampler import golden_jsonl, stratified_sample

    keep = _csv(args.keep)
    try:
        with open(args.inp, encoding="utf-8") as fh:
            rows, summary = stratified_sample(
                fh,
                stratum_field=args.stratum_field,
                keep=keep,
                negatives=args.negatives,
                request_field=args.request_field,
                id_field=args.id_field,
                reference_field=args.reference_field,
                cap_per_stratum=args.cap_per_stratum,
                seed=args.seed,
            )
    except (OSError, ValueError) as exc:
        _err(f"caliper sample: {exc}")
        return EXIT_USAGE
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(golden_jsonl(rows))

    print(
        f"sampled {summary.n_selected} of {summary.n_lines} log lines "
        f"(seed {args.seed}; kept whole: {', '.join(keep) or 'none'})"
    )
    for stratum, available in summary.by_stratum.items():
        chosen = summary.selected_by_stratum.get(stratum, 0)
        print(f"  {stratum:<16} {chosen:>6} / {available}")
    if summary.n_skipped:
        reasons = ", ".join(f"{k}: {v}" for k, v in summary.skipped_reasons.items())
        _err(f"skipped {summary.n_skipped} lines ({reasons})")
    print(f"wrote {args.out}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="caliper",
        description="Know when a model or prompt change breaks your AI feature.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("eval", help="run candidates over a golden set and report")
    e.add_argument("--config", required=True, help="eval TOML file")
    e.add_argument("--out", default="caliper-out", help="output dir (default: caliper-out)")
    e.add_argument("--budget-usd", type=float, default=None, help="override budget_usd")
    e.add_argument("--no-resume", action="store_true", help="start the results file fresh")
    e.add_argument("--retry-errors", action="store_true", help="re-run rows that errored")
    e.add_argument("--skip-preflight", action="store_true")
    e.add_argument("--ci", action="store_true", help="exit 1 when the gate fails")
    e.add_argument("--summary-md", default=None, help="write a PR-comment markdown summary")
    e.add_argument("--candidates", default=None, help="comma-separated subset to run")
    e.add_argument(
        "--target-precision",
        type=float,
        default=None,
        help="recommend the lowest threshold reaching this precision "
        "(default: [calibration].target_precision)",
    )
    e.add_argument(
        "--max-error-rate",
        type=float,
        default=None,
        help="CI error-rate allowance (default: [ci].max_error_rate or 0)",
    )
    e.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples (default 1000)")
    e.add_argument(
        "--test-ledger",
        default=None,
        help="test-split touch ledger (default: OUT/test_ledger.jsonl)",
    )
    e.add_argument(
        "--allow-test-retouch",
        action="store_true",
        help="do not fail --ci when a candidate's test split was already reported",
    )
    e.set_defaults(func=cmd_eval)

    s = sub.add_parser("sample", help="build a golden set from production logs")
    s.add_argument("--in", dest="inp", required=True, help="JSONL logs")
    s.add_argument("--out", required=True, help="golden JSONL to write")
    s.add_argument("--stratum-field", required=True, help="dotted path, e.g. outcome")
    s.add_argument("--keep", required=True, help="strata kept whole, e.g. positive,unsure")
    s.add_argument("--negatives", type=int, required=True, help="rows sampled from the rest")
    s.add_argument("--request-field", default="request", help="dotted path to the request")
    s.add_argument("--id-field", default=None, help="dotted path to a row id")
    s.add_argument("--reference-field", default=None, help="dotted path -> reference_outcome")
    s.add_argument("--cap-per-stratum", type=int, default=None)
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_sample)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
