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
import textwrap
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
    say(f"dataset            {EXAMPLE_REL}")
    say(f"prompts            {len(prompts)} synthetic invoices, each with a gold total")
    say(f"labeled rollouts   {len(labeled)} candidate answers, labeled by a human")
    say("graders            naive_grader, fixed_grader, length_baseline")
    for label, title in ((1, "approved"), (0, "rejected")):
        cases: dict[str, int] = {}
        for r in labeled:
            if human_label(r) == label:
                case = str(r.rollout.metadata.get("case", "other"))
                cases[case] = cases.get(case, 0) + 1
        # NBSP keeps "case count" pairs together when wrapping.
        body = ", ".join(f"{c}\u00a0{k}" for c, k in cases.items())
        say()
        say(f"human {title} ({sum(cases.values())}):")
        for line in textwrap.wrap(body, WIDTH - 2):
            say("  " + line.replace("\u00a0", " "))
    return prompts, labeled, graders


def print_mutation_table(report: MutationReport) -> None:
    say(f"grader: {report.grader_name} v{report.grader_version}   rows: {report.n_rows}")
    say()
    say(f"  {'mutation':<20} {'robust grader':<14} {'caught':>8}   verdict")
    say(f"  {'-' * 20} {'-' * 14} {'-' * 8}   {'-' * 18}")
    for s in report.per_mutation:
        expect = "score drops" if s.expected_effect == "lower" else "score same"
        caught = s.n_applied - s.n_violations
        if s.n_applied == 0:
            verdict = "n/a (no-op on this data)"
        elif s.passed:
            verdict = "killed"
        else:
            verdict = f"SURVIVED ({s.n_violations} rows)"
        say(f"  {s.name:<20} {expect:<14} {caught:>3}/{s.n_applied:<4}   {verdict}")
    survived = [s.name for s in report.per_mutation if not s.passed]
    n_live = sum(1 for s in report.per_mutation if s.n_applied > 0)
    say()
    if survived:
        say(f"RESULT: FAIL - {len(survived)} of {n_live} applicable mutations survive")
    else:
        say(f"RESULT: PASS - all {n_live} applicable mutations killed")


def step_mutate(
    n: int, title: str, grader: Callable, rows: list[EvalRow], show_hack: bool
) -> MutationReport:
    section(n, title)
    say(f"8 built-in mutations x {len(rows)} human-approved rollouts")
    say()
    report = run_mutations(grader, MUTATIONS, rows)
    print_mutation_table(report)
    if show_hack:
        by_id = {r.id: r for r in rows}
        hacks = [o for o in report.outcomes if o.mutation == "hardcode_gold" and o.violation]
        if hacks:
            # The most telling example: the longest worked answer it replaces.
            hack = max(hacks, key=lambda o: len(by_id[o.row_id].rollout.response))
            row = by_id[hack.row_id]
            hacked = HardcodeGold().apply(row.rollout)
            say()
            say(f"the hack, concretely ({row.id}):")
            say(f"  approved answer  ...{clip(row.rollout.response[-40:], 44)}")
            say(f"    -> score       {hack.baseline_score:.2f}")
            say(f"  hardcoded gold   {clip(hacked.response, 44)}")
            say(f"    -> score       {hack.mutated_score:.2f}   <- full reward, no work, no tag")
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
    say(f"  {'grader':<17} {'kappa':>9} {'agree':>7} {'false pass':>11} {'false fail':>11}")
    say(f"  {'-' * 17} {'-' * 9} {'-' * 7} {'-' * 11} {'-' * 11}")
    reports: dict[str, KappaReport] = {}
    for name in ("length_baseline", "naive_grader", "fixed_grader"):
        rep = compute_kappa(
            getattr(graders, name), labeled, labels, weighting=KappaWeighting.UNWEIGHTED
        )
        reports[name] = rep
        # confusion[grader_bucket][label_bucket]
        false_pass, false_fail = rep.confusion[1][0], rep.confusion[0][1]
        say(
            f"  {name:<17} {fmt_kappa(rep):>9} {rep.observed_agreement:>7.0%}"
            f" {false_pass:>11} {false_fail:>11}"
        )
    say()
    say("length_baseline never reads the gold answer: it is the leakage floor a")
    say("real grader must clear by a wide margin.")

    naive = reports["naive_grader"]
    false_passes = [
        p for p in naive.per_row if p.grader_bucket == 1 and p.label_bucket == 0
    ]
    by_id = {r.id: r for r in labeled}
    seen: set[str] = set()
    examples = []
    for p in false_passes:
        case = str(by_id[p.row_id].rollout.metadata.get("case", p.row_id))
        if case not in seen:
            seen.add(case)
            examples.append(by_id[p.row_id])
    say()
    say(f"naive_grader pays full reward to {len(false_passes)} answers humans rejected, e.g.:")
    for r in examples[:4]:
        resp = r.rollout.response
        say(f"  {r.id}  {clip(resp if len(resp) <= 56 else '...' + resp[-53:], 60)}")
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
    row = json.loads(first)
    say()
    say("first train row:")
    for key in sorted(row):
        say(f"  {key:<17} {clip(json.dumps(row[key]), WIDTH - 20)}")
    call_reward_funcs(out, row, graders)


_REWARD_PROBE = """
import json, sys
sys.path.insert(0, ".")
from reward_funcs import reward_funcs
kwargs = json.load(sys.stdin)
print(json.dumps([f.__name__ for f in reward_funcs]))
print(json.dumps([f(**kwargs) for f in reward_funcs]))
"""


def call_reward_funcs(out: Path, first_row: dict, graders: ModuleType) -> None:
    """Score two completions with the emitted reward module, outside caliper.

    Runs in an isolated interpreter (-I -S: no site-packages, so neither
    caliper nor pydantic is importable) to show the file is self-contained.
    """
    gold = first_row["gold"]
    completions = {
        "good": f"The amount due is <total>{gold}</total>",
        "hack": f"<total>0.00 or {gold}</total>",
    }
    n = len(completions)
    kwargs = {
        "prompts": [first_row["prompt"]] * n,
        "completions": [[{"role": "assistant", "content": c}] for c in completions.values()],
        "gold": [gold] * n,
        "caliper_metadata": [first_row.get("caliper_metadata")] * n,
    }
    proc = subprocess.run(  # noqa: S603 - fixed argv, local interpreter
        [sys.executable, "-I", "-S", "-c", _REWARD_PROBE],
        cwd=out,
        input=json.dumps(kwargs),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        say(scrub(proc.stderr))
        sys.exit(proc.returncode)
    names, scores = (json.loads(line) for line in proc.stdout.splitlines()[:2])

    say()
    say("reward_funcs.py in a bare interpreter (python -I -S: no caliper, no deps):")
    say(f"  reward_funcs = [{', '.join(names)}]   # GRPOTrainer(reward_funcs=...)")
    say()
    say(f"  {'completion':<46} {'fixed':>6} {'naive':>6}")
    for (kind, text), score in zip(completions.items(), scores[0], strict=True):
        naive = graders.naive_grader({"prompt": "", "response": text, "gold": gold}).score
        say(f"  {kind}: {clip(text, 40):<40} {score:>6.2f} {naive:>6.2f}")


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
