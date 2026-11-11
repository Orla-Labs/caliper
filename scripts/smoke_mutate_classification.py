"""Smoke: classification-grader mutations, naive vs robust grader.

A fictional support desk (Pinecone Outfitters, not a real company) triages
tickets into ``refund`` / ``shipping`` / ``account``. The model answers with
JSON ``{"verdict": ..., "rationale": ...}``. Two graders score that output:

* ``naive_classifier_grader`` - 1.0 when the gold label appears as a word in
  the verdict. Easy to write, and it lets through every degenerate classifier
  below.
* ``robust_classifier_grader`` - generic properties of a good classification:
  exactly one allowed label; the label equals gold; the rationale quotes at
  least 4 consecutive words of the ticket as evidence; and the last label the
  rationale names agrees with the verdict.

The five classification mutations run on the correct, human-approved outputs
(baseline 1.0 for both graders), so any mutation that does not lower the score
"survives". Expected: the naive grader lets all 5 survive; the robust grader
kills all 5.

No network, no API keys. Run from the repo root:
    uv run python scripts/smoke_mutate_classification.py
"""

from __future__ import annotations

import json
import re
import sys

from caliper_mutate import (
    MutationReport,
    classification_mutations,
    run_mutations,
)
from caliper_sdk import EvalRow, GraderResult, Rollout, grader

LABELS = ["refund", "shipping", "account"]
KEYWORDS = {"refund": "refund", "package": "shipping", "password": "account"}

# (ticket, gold, evidence quote from the ticket)
TICKETS: list[tuple[str, str, str]] = [
    (
        "The hiking boots arrived with a torn sole. I would like a refund to my card, please.",
        "refund",
        "I would like a refund",
    ),
    (
        "I returned the tent last week. When will the money for it be back on my card?",
        "refund",
        "the money for it be back",
    ),
    (
        "Please refund the rain jacket, it is two sizes too small and I sent it back.",
        "refund",
        "it is two sizes too small",
    ),
    (
        "My package was marked delivered but nothing is at my door or with my neighbours.",
        "shipping",
        "nothing is at my door",
    ),
    (
        "I do not want a refund. I just need to know where my parcel is; it is four days late.",
        "shipping",
        "where my parcel is; it is four days late",
    ),
    (
        "I cannot reset my password and the login page keeps saying my email is unknown.",
        "account",
        "the login page keeps saying",
    ),
]

_LABEL_RE = re.compile(r"\b(" + "|".join(LABELS) + r")\b")
_QUOTE_RE = re.compile(r'"([^"]+)"')


def build_rows() -> list[EvalRow]:
    rows = []
    for i, (ticket, gold, quote) in enumerate(TICKETS):
        response = json.dumps(
            {"verdict": gold, "rationale": f'The customer writes "{quote}", so this is {gold}.'}
        )
        prompt = f"Classify this support ticket as refund, shipping or account.\n\n{ticket}"
        rows.append(
            EvalRow(id=f"t{i}", rollout=Rollout(prompt=prompt, response=response, gold=gold))
        )
    return rows


def _parse(response: str) -> tuple[str, str] | None:
    try:
        obj = json.loads(response)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return str(obj.get("verdict", "")), str(obj.get("rationale", ""))


@grader(name="naive_classifier_grader", version="0.1.0")
def naive_classifier_grader(rollout: Rollout) -> GraderResult:
    parsed = _parse(rollout.response)
    if parsed is None or rollout.gold is None:
        return GraderResult(score=0.0, explanation="unparseable")
    words = re.findall(r"[a-z_]+", parsed[0].lower())
    ok = rollout.gold in words
    return GraderResult(score=1.0 if ok else 0.0, explanation="gold in verdict" if ok else "miss")


@grader(name="robust_classifier_grader", version="0.1.0")
def robust_classifier_grader(rollout: Rollout) -> GraderResult:
    parsed = _parse(rollout.response)
    if parsed is None or rollout.gold is None:
        return GraderResult(score=0.0, explanation="unparseable")
    verdict, rationale = parsed[0].strip().lower(), parsed[1]
    if verdict not in LABELS:
        return GraderResult(score=0.0, explanation="verdict is not exactly one allowed label")
    if verdict != rollout.gold:
        return GraderResult(score=0.0, explanation="wrong label")
    evidence = [q for q in _QUOTE_RE.findall(rationale) if len(q.split()) >= 4]
    if not any(q in rollout.prompt for q in evidence):
        return GraderResult(score=0.5, explanation="right label, no quoted evidence")
    named = _LABEL_RE.findall(rationale.lower())
    if named and named[-1] != verdict:
        return GraderResult(score=0.5, explanation="rationale concludes a different label")
    return GraderResult(score=1.0, explanation="right label, justified")


def survivors(report: MutationReport) -> list[str]:
    return [s.name for s in report.per_mutation if s.n_violations or s.n_failures]


def run() -> dict[str, MutationReport]:
    rows = build_rows()
    mutations = classification_mutations(
        LABELS, keywords=KEYWORDS, eval_set=rows, constant_label="shipping"
    )
    return {
        g.name: run_mutations(g, mutations, rows)
        for g in (naive_classifier_grader, robust_classifier_grader)
    }


def main() -> int:
    reports = run()
    n_rows = len(build_rows())
    print(f"[smoke] fictional ticket triage: {n_rows} rows, labels {', '.join(LABELS)}")
    for name, report in reports.items():
        print(f"\n{name}")
        for s in report.per_mutation:
            verdict = "SURVIVES" if (s.n_violations or s.n_failures) else "killed"
            total = s.n_applied + s.n_skipped_no_op
            print(
                f"  {s.name:<32} applied {s.n_applied}/{total}  "
                f"violations {s.n_violations}  {verdict}"
            )
        print(f"  survivors: {len(survivors(report))}/{len(report.per_mutation)}")

    naive = reports["naive_classifier_grader"]
    robust = reports["robust_classifier_grader"]
    ok = (
        len(survivors(naive)) == len(naive.per_mutation)
        and not survivors(robust)
        and all(s.n_applied > 0 for s in robust.per_mutation)
    )
    print("\n[smoke] " + ("OK" if ok else "UNEXPECTED RESULT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
