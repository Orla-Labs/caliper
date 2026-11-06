"""Verify the invoice_totals graders against the real Caliper APIs.

Runs all 8 built-in mutations and Cohen's kappa (vs ``metadata.human_label``)
for each grader on labeled_rollouts.jsonl, prints a summary, and asserts:

* naive_grader has surviving mutations (it is reward-hackable),
* fixed_grader has none,
* kappa ordering: length_baseline < naive_grader < fixed_grader.

Run from the repo root:

    uv run python examples/invoice_totals/check.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from caliper_kappa import LabeledRow, LabelSet, compute_kappa
from caliper_mutate import (
    AppendGarbage,
    CasePerturb,
    DropFormatGate,
    HardcodeGold,
    PrependSycophancy,
    TruncateResponse,
    WhitespacePerturb,
    WrongAnswer,
    run_mutations,
)
from caliper_sdk import EvalRow

HERE = Path(__file__).resolve().parent


def _load_graders():
    spec = importlib.util.spec_from_file_location("invoice_totals_graders", HERE / "graders.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_rows(name: str) -> list[EvalRow]:
    with (HERE / name).open(encoding="utf-8") as fh:
        return [EvalRow.model_validate(json.loads(line)) for line in fh if line.strip()]


def main() -> int:
    g = _load_graders()
    graders = [g.length_baseline, g.naive_grader, g.fixed_grader]
    prompts = _load_rows("prompts.jsonl")
    rows = _load_rows("labeled_rollouts.jsonl")
    labels = LabelSet(
        name="human",
        rows=[
            LabeledRow(row_id=r.id, label_score=float(r.rollout.metadata["human_label"]))
            for r in rows
        ],
    )
    mutations = [
        DropFormatGate(),
        WrongAnswer(),
        TruncateResponse(),
        WhitespacePerturb(),
        PrependSycophancy(),
        AppendGarbage(),
        CasePerturb(),
        HardcodeGold(),
    ]

    n_pos = sum(int(lab.label_score) for lab in labels.rows)
    print(f"prompts: {len(prompts)}  labeled rollouts: {len(rows)}  human_label=1: {n_pos}")
    print()

    kappas: dict[str, float] = {}
    survivors: dict[str, list[str]] = {}
    for grader in graders:
        report = run_mutations(grader, mutations, rows)
        kr = compute_kappa(grader, rows, labels)
        assert not kr.kappa_undefined, grader.name
        assert not report.failures, (grader.name, report.failures)
        kappas[grader.name] = kr.kappa
        survivors[grader.name] = [s.name for s in report.per_mutation if not s.passed]
        print(f"{grader.name}")
        print(
            f"  kappa={kr.kappa:.3f}  raw_agreement={kr.observed_agreement:.3f}  "
            f"chance_agreement={kr.expected_agreement:.3f}  confusion={kr.confusion}"
        )
        for s in report.per_mutation:
            status = "PASS" if s.passed else "FAIL"
            print(
                f"  {s.name:<20} {s.expected_effect:<6} applied={s.n_applied:<3} "
                f"no_op={s.n_skipped_no_op:<3} violations={s.n_violations:<3} {status}"
            )
        print(f"  surviving mutations: {len(survivors[grader.name])}")
        print()

    ok = True
    if not survivors["naive_grader"]:
        print("CHECK FAILED: naive_grader should have surviving mutations")
        ok = False
    if survivors["fixed_grader"]:
        print(f"CHECK FAILED: fixed_grader has surviving mutations {survivors['fixed_grader']}")
        ok = False
    if not (kappas["length_baseline"] < kappas["naive_grader"] < kappas["fixed_grader"]):
        print(f"CHECK FAILED: kappa ordering {kappas}")
        ok = False
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
