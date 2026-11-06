"""``caliper-compile``: compile a caliper grader + dataset for a training target.

    caliper-compile --target {trl,bedrock,openai} --grader PATH.py[:ATTR] \\
        --dataset PATH.jsonl --out DIR [--on-error SCORE|none]

Output is deterministic (byte-identical across runs for the same inputs).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from caliper_rft.targets import TARGETS, CompileError, GraderSource, compile_target, load_dataset
from caliper_rft.targets._common import rel_display

OPENAI_DEPRECATION = (
    "warning: the 'openai' target is LEGACY and deprecated. OpenAI is winding down its "
    "fine-tuning platform (closed to new users; existing customers lose job creation on "
    "2027-01-06): https://developers.openai.com/api/docs/deprecations. "
    "Prefer --target trl or --target bedrock."
)
BEDROCK_MIN_ROWS = 100  # https://docs.aws.amazon.com/bedrock/latest/userguide/rft-prepare-data.html


def _parse_on_error(value: str) -> float | None:
    if value.lower() == "none":
        return None
    try:
        f = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("--on-error must be a float in [0, 1] or 'none'") from e
    if not 0.0 <= f <= 1.0:
        raise argparse.ArgumentTypeError("--on-error must be within [0, 1]")
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="caliper-compile",
        description="Compile a caliper @grader + EvalRow dataset into training-target artifacts.",
    )
    p.add_argument("--target", required=True, choices=TARGETS)
    p.add_argument("--grader", required=True, metavar="PATH.py[:ATTR]",
                   help="Python file and the @grader-decorated attribute to use")
    p.add_argument("--dataset", required=True, metavar="PATH.jsonl",
                   help="EvalRow JSONL ({id, rollout: {prompt, response, gold, metadata}})")
    p.add_argument("--out", required=True, metavar="DIR", help="output directory")
    p.add_argument("--on-error", type=_parse_on_error, default=0.0, metavar="SCORE",
                   help="score when the grader raises/returns an invalid score "
                        "(default 0.0; 'none' = TRL skips the sample; trl target only)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.target == "openai":
        print(OPENAI_DEPRECATION, file=sys.stderr)
    if args.on_error is None and args.target != "trl":
        print("error: --on-error none is only valid for --target trl", file=sys.stderr)
        return 2
    try:
        grader = GraderSource.from_spec(args.grader)
        rows = load_dataset(args.dataset)
        written = compile_target(args.target, grader, rows, args.out, on_error=args.on_error)
    except CompileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.target == "bedrock" and len(rows) < BEDROCK_MIN_ROWS:
        print(
            f"warning: Bedrock RFT requires at least {BEDROCK_MIN_ROWS} training records; "
            f"dataset has {len(rows)}",
            file=sys.stderr,
        )
    print(f"caliper-compile: target={args.target} grader={grader.attr} "
          f"(name={grader.name}) rows={len(rows)}")
    for name in written:
        print(f"  wrote {rel_display(args.out, name)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
