"""Caliper end-to-end demo: catch a reward hack before your model does.

Deterministic by construction: no network, no LLM calls, no randomness,
no timestamps, no absolute paths in the output. `scripts/demo.sh --check`
diffs a fresh run against `demo/expected_output.txt`.

Arc:
  1. load the golden dataset (examples/invoice_totals)
  2. mutation-test the naive grader  -> mutants survive (reward-hackable)
  3. mutation-test the fixed grader  -> mutants killed
  4. Cohen's kappa vs human labels   -> length baseline < naive < fixed
  5. compile the fixed grader to TRL -> call the emitted reward function
"""

from __future__ import annotations

import os
import sys

# The built-in whitespace/case mutations seed their RNG from `hash(str)`,
# which Python salts per process. Pin the salt so every run is identical.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606

import importlib.util
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from caliper_kappa import (
    KappaReport,
    KappaWeighting,
    LabeledRow,
    LabelSet,
    compute_kappa,
)
from caliper_mutate import (
    AppendGarbage,
    CasePerturb,
    DropFormatGate,
    HardcodeGold,
    MutationReport,
    PrependSycophancy,
    TruncateResponse,
    WhitespacePerturb,
    WrongAnswer,
    run_mutations,
)
from caliper_sdk import EvalRow

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = Path(os.environ.get("CALIPER_DEMO_EXAMPLE_DIR", ROOT / "examples" / "invoice_totals"))
EXAMPLE_REL = "examples/invoice_totals"
BUILD_REL = "build/demo-trl"
WIDTH = 72

MUTATIONS = [
    DropFormatGate(),
    WrongAnswer(),
    TruncateResponse(),
    HardcodeGold(),
    WhitespacePerturb(),
    PrependSycophancy(),
    AppendGarbage(),
    CasePerturb(),
]


# --------------------------------------------------------------------------- output


def say(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def section(n: int, title: str) -> None:
    say()
    say("=" * WIDTH)
    say(f" [{n}/5] {title}")
    say("=" * WIDTH)


def clip(text: str, n: int = 60) -> str:
    flat = text.replace("\n", "\\n")
    return flat if len(flat) <= n else flat[: n - 3] + "..."


def scrub(text: str) -> str:
    """Remove machine-specific paths and ANSI colour from subprocess output."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace(str(ROOT) + os.sep, "").replace(str(ROOT), ".")
    return text


# --------------------------------------------------------------------------- data


def load_rows(path: Path) -> list[EvalRow]:
    with path.open(encoding="utf-8") as fh:
        return [EvalRow.model_validate_json(line) for line in fh if line.strip()]


def load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def human_label(row: EvalRow) -> int:
    return int(row.rollout.metadata["human_label"])


# --------------------------------------------------------------------------- steps


def step_load() -> tuple[list[EvalRow], list[EvalRow], ModuleType]:
    section(1, "Load the golden dataset")
    prompts = load_rows(EXAMPLE_DIR / "prompts.jsonl")
    labeled = load_rows(EXAMPLE_DIR / "labeled_rollouts.jsonl")
    graders = load_module(EXAMPLE_DIR / "graders.py", "invoice_totals_graders")
    n_good = sum(1 for r in labeled if human_label(r) == 1)
    say(f"dataset            {EXAMPLE_REL}")
    say(f"prompts            {len(prompts)} invoices with gold totals")
    say(f"labeled rollouts   {len(labeled)} candidate responses, human-labeled")
    say(f"  human: good      {n_good}")
    say(f"  human: bad       {len(labeled) - n_good}")
    say("graders            naive_grader, fixed_grader, length_baseline")
    example = next(r for r in labeled if human_label(r) == 1)
    say()
    say(f"example row        {example.id}")
    say(f"  prompt           {clip(example.rollout.prompt)}")
    say(f"  response         {clip(example.rollout.response)}")
    say(f"  gold             {example.rollout.gold}")
    return prompts, labeled, graders


def print_mutation_table(report: MutationReport) -> None:
    say(f"grader: {report.grader_name} v{report.grader_version}   rows: {report.n_rows}")
    say()
    say(f"  {'mutation':<20} {'robust grader':<14} {'caught':>8}   verdict")
    say(f"  {'-' * 20} {'-' * 14} {'-' * 8}   {'-' * 18}")
    for s in report.per_mutation:
        expect = "score drops" if s.expected_effect == "lower" else "score same"
        caught = s.n_applied - s.n_violations
        verdict = "killed" if s.passed else f"SURVIVED ({s.n_violations} rows)"
        say(f"  {s.name:<20} {expect:<14} {caught:>3}/{s.n_applied:<4}   {verdict}")
    survived = [s.name for s in report.per_mutation if not s.passed]
    say()
    if survived:
        say(f"RESULT: FAIL - {len(survived)}/{len(report.per_mutation)} mutations survive: "
            + ", ".join(survived))
    else:
        say(f"RESULT: PASS - all {len(report.per_mutation)} mutations killed")


def step_mutate(
    n: int, title: str, grader: Callable, rows: list[EvalRow], show_hack: bool
) -> MutationReport:
    section(n, title)
    say(f"8 built-in mutations x {len(rows)} human-approved rollouts")
    say()
    report = run_mutations(grader, MUTATIONS, rows)
    print_mutation_table(report)
    if show_hack:
        hack = next(
            (o for o in report.outcomes if o.mutation == "hardcode_gold" and o.violation),
            None,
        )
        if hack is not None:
            row = next(r for r in rows if r.id == hack.row_id)
            hacked = HardcodeGold().apply(row.rollout)
            say()
            say("the hack, concretely:")
            say(f"  original response  {clip(row.rollout.response)}")
            say(f"    -> score         {hack.baseline_score:.2f}")
            say(f"  hacked response    {clip(hacked.response)}")
            say(f"    -> score         {hack.mutated_score:.2f}   <- full reward, zero work")
    return report


def fmt_kappa(rep: KappaReport) -> str:
    return "undefined" if rep.kappa_undefined else f"{rep.kappa:+.3f}"


def step_kappa(labeled: list[EvalRow], graders: ModuleType) -> dict[str, KappaReport]:
    section(4, "Agreement with human labels (Cohen's kappa)")
    labels = LabelSet(
        name="human",
        rows=[LabeledRow(row_id=r.id, label_score=float(human_label(r))) for r in labeled],
    )
    say(f"{len(labeled)} labeled rollouts, pass/fail buckets split at 0.5")
    say()
    say(f"  {'grader':<17} {'kappa':>9} {'agree':>7}   confusion [[TN FN] [FP TP]]")
    say(f"  {'-' * 17} {'-' * 9} {'-' * 7}   {'-' * 28}")
    reports: dict[str, KappaReport] = {}
    for name in ("length_baseline", "naive_grader", "fixed_grader"):
        rep = compute_kappa(
            getattr(graders, name), labeled, labels, weighting=KappaWeighting.UNWEIGHTED
        )
        reports[name] = rep
        (tn, fn), (fp, tp) = rep.confusion
        say(
            f"  {name:<17} {fmt_kappa(rep):>9} {rep.observed_agreement:>6.0%}"
            f"   [[{tn:>2} {fn:>2}] [{fp:>2} {tp:>2}]]"
        )
    say()
    say("length_baseline is the leakage floor: a grader that only looks at surface")
    say("features. A real grader has to beat it by a wide margin.")
    return reports


def step_compile(graders: ModuleType) -> None:
    section(5, "Compile fixed_grader to TRL (GRPOTrainer)")
    out = ROOT / BUILD_REL
    shutil.rmtree(out, ignore_errors=True)
    exe = Path(sys.executable).with_name("caliper-compile")
    args = [
        "--target", "trl",
        "--grader", f"{EXAMPLE_REL}/graders.py:fixed_grader",
        "--dataset", f"{EXAMPLE_REL}/prompts.jsonl",
        "--out", BUILD_REL,
    ]
    say("$ uv run caliper-compile \\")
    for flag, value in zip(args[::2], args[1::2], strict=True):
        say(f"    {flag} {value}" + ("" if flag == "--out" else " \\"))
    proc = subprocess.run(  # noqa: S603 - fixed argv, local workspace binary
        [str(exe), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    for line in scrub(proc.stdout + proc.stderr).splitlines():
        say(f"  {line}")
    if proc.returncode != 0:
        say(f"caliper-compile exited {proc.returncode}")
        sys.exit(proc.returncode)

    say()
    say(f"emitted files in {BUILD_REL}/:")
    for f in sorted(p for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        n_lines = len(f.read_text(encoding="utf-8").splitlines())
        say(f"  {f.relative_to(out).as_posix():<20} {n_lines:>4} lines")

    first = (out / "train.jsonl").read_text(encoding="utf-8").splitlines()[0]
    say()
    say("first train row:")
    say(f"  {clip(first, WIDTH - 2)}")
    call_reward_funcs(out, json.loads(first), graders)


def call_reward_funcs(out: Path, first_row: dict, graders: ModuleType) -> None:
    """Load the emitted reward module standalone and score two completions."""
    raise NotImplementedError("wired up once dev1's reward_funcs.py shape is known")


# --------------------------------------------------------------------------- main


def main() -> int:
    say("Caliper demo: catch reward hacks before your model does")
    _, labeled, graders = step_load()
    good_rows = [r for r in labeled if human_label(r) == 1]
    step_mutate(2, "Mutation-test naive_grader", graders.naive_grader, good_rows, True)
    step_mutate(3, "Mutation-test fixed_grader", graders.fixed_grader, good_rows, False)
    step_kappa(labeled, graders)
    step_compile(graders)
    say()
    say("Done. Same grader, same verdicts, from CI to the training loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
