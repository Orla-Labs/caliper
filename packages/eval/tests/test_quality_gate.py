"""Quality against human approvals, and the ``[ci] quality_per_cost`` gate.

The rule under test: a candidate that costs X% more than the reference must make
at least X% fewer errors than the reference (times ``quality_per_cost``), where
"error" means a person rejected that outcome for that row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caliper_eval.approvals import Approval, build_verdicts, read_approvals
from caliper_eval.ci import evaluate_gate
from caliper_eval.cli import main
from caliper_eval.config import config_from_dict
from caliper_eval.ledger import record_touches
from caliper_eval.quality import compute_quality
from caliper_eval.report import build_report, render_markdown, render_text
from caliper_eval.review import plan_review
from caliper_eval.types import (
    Decision,
    GoldenRow,
    LabelSource,
    Reply,
    RowResult,
    Split,
    Usage,
    request_sha256,
)

GOOD, BAD = "good", "bad"


def _req(row: str) -> dict:
    return {"messages": [{"role": "user", "content": row}]}


def _result(cand: str, row: str, replica: int, outcome: str | None, cost: float) -> RowResult:
    return RowResult(
        run_id="test",
        candidate=cand,
        row_id=row,
        replica=replica,
        request_sha256=request_sha256(_req(row)),
        replies=[Reply(text="x", usage=Usage(input_tokens=100), cost_usd=cost)],
        decision=None if outcome is None else Decision(outcome=outcome),
    )


def _world(
    n: int,
    ref_wrong: int,
    *,
    fixes: int = 0,
    breaks: int = 0,
    cand_cost: float = 0.001,
    labelled: int | None = None,
    ci: dict | None = None,
    quality: dict | None = None,
):
    """``n`` rows whose correct outcome is GOOD. The reference decides BAD on the first
    ``ref_wrong`` rows. The candidate copies it, except that it decides GOOD on the
    first ``fixes`` of those rows and BAD on ``breaks`` rows the reference got right.
    Every row (or the first ``labelled``) carries a human golden label."""
    rows = [f"r{i:04d}" for i in range(n)]
    labelled = n if labelled is None else labelled
    golden = [
        GoldenRow(
            id=r,
            request=_req(r),
            label=GOOD if i < labelled else None,
            label_source=LabelSource.INTERNAL_REVIEWER if i < labelled else None,
            split=Split.TEST if i % 2 else Split.VAL,
        )
        for i, r in enumerate(rows)
    ]
    ref_out = [BAD if i < ref_wrong else GOOD for i in range(n)]
    cand_out = list(ref_out)
    for i in range(fixes):
        cand_out[i] = GOOD
    for j in range(breaks):
        cand_out[ref_wrong + j] = BAD
    results = [_result("ref", r, k, ref_out[i], 0.001) for i, r in enumerate(rows) for k in (0, 1)]
    results += [_result("cand", r, 0, cand_out[i], cand_cost) for i, r in enumerate(rows)]
    config = config_from_dict(
        {
            "golden": "g.jsonl",
            "reference": "ref",
            "candidates": [
                {"name": "ref", "provider": "fake", "model": "ref"},
                {"name": "cand", "provider": "fake", "model": "cand"},
            ],
            "ci": {"max_regression": 1.0, **(ci or {})},
            "quality": quality if quality is not None else {},
        }
    )
    return config, results, golden


def _gate(config, results, golden, approvals=()):
    report = build_report(config, results, golden, n_boot=400, approvals=approvals)
    gate = evaluate_gate(report)
    return report, next(c for c in gate.candidates if c.name == "cand")


# --------------------------------------------------------------------------- approvals


def test_approvals_read_native_and_label_export_lines(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    p.write_text(
        json.dumps({"row_id": "r1", "outcome": "good", "approved": True})
        + "\n\n"
        # a `caliper label export --format jsonl` row of a `caliper review` project
        + json.dumps(
            {
                "item_id": "abc",
                "label": "no",
                "approved": False,
                "source": "internal_reviewer",
                "labeller": "ana",
                "item": {"row_id": "r2", "outcome": "bad", "request_sha256": "x"},
            }
        )
        + "\n"
    )
    a, b = read_approvals(p)
    assert (a.row_id, a.outcome, a.approved, a.source) == ("r1", "good", True, "internal_reviewer")
    assert (b.row_id, b.outcome, b.approved, b.reviewer, b.request_sha256) == (
        "r2",
        "bad",
        False,
        "ana",
        "x",
    )
    p.write_text('{"row_id": "r1"}\n')
    with pytest.raises(ValueError, match=r"a\.jsonl:1: invalid approval"):
        read_approvals(p)


def test_only_current_human_verdicts_count_and_conflicts_void_the_pair():
    golden = [
        GoldenRow(id="r1", request=_req("r1")),
        GoldenRow(
            id="r2", request=_req("r2"), label=GOOD, label_source=LabelSource.INTERNAL_REVIEWER
        ),
        GoldenRow(id="r3", request=_req("r3"), label=GOOD, label_source=LabelSource.LLM_TEACHER),
    ]
    sha1 = request_sha256(_req("r1"))
    lines = [
        Approval(row_id="r1", outcome="a", approved=True, request_sha256=sha1),
        Approval(row_id="r1", outcome="b", approved=True, source=LabelSource.LLM_TEACHER),
        Approval(row_id="r1", outcome="c", approved=None),  # unsure
        Approval(row_id="r1", outcome="d", approved=True, request_sha256="old"),  # stale
        Approval(row_id="nope", outcome="a", approved=True),
        Approval(row_id="r1", outcome="e", approved=True),
        Approval(row_id="r1", outcome="e", approved=False),  # contradicts the line above
        Approval(row_id="r2", outcome=GOOD, approved=False),  # contradicts the golden label
    ]
    v, s = build_verdicts(golden, lines)
    assert v.get("r1", "a") is True
    assert v.get("r1", "b") is None and v.get("r1", "c") is None and v.get("r1", "d") is None
    assert v.get("r1", "e") is None
    assert v.get("r2", GOOD) is None  # the conflict voids the label's verdict too
    assert v.get("r2", BAD) is False  # the label still rejects every other outcome
    assert v.get("r3", GOOD) is None  # teacher labels are not evidence
    assert (s.used, s.not_human, s.unsure, s.stale, s.unknown_rows, s.conflicts) == (
        4,
        1,
        1,
        1,
        1,
        2,
    )
    v2, s2 = build_verdicts(golden, [], golden_labels=False)
    assert v2.get("r2", GOOD) is None and s2.from_golden_labels == 0


# --------------------------------------------------------------------------- the measure


def test_same_outcomes_add_nothing_and_changes_are_counted():
    _config, results, golden = _world(100, 30, fixes=6, breaks=2)
    v, _ = build_verdicts(golden, [])
    q = compute_quality(results, v, "cand", "ref", seed=1, n_boot=200)
    assert (q.n_rows, q.n_changed, q.fixed, q.broken, q.both_approved, q.both_rejected) == (
        100,
        8,
        6,
        2,
        0,
        0,
    )
    assert q.n_same == 92 and q.n_same_reviewed == 92
    assert q.improvement.estimate == pytest.approx(0.04)  # (6 - 2) / 100
    assert q.reference_error_rate.estimate == pytest.approx(0.30)
    assert q.candidate_error_rate.estimate == pytest.approx(0.26)
    assert q.relative_improvement.estimate == pytest.approx(0.04 / 0.30)
    again = compute_quality(results, v, "cand", "ref", seed=1, n_boot=200)
    assert again == q  # deterministic


def test_an_unreviewed_change_is_unknown_not_a_tie():
    # Rows past `labelled` have no verdict; the candidate changes one of them.
    _config, results, golden = _world(20, 0, breaks=20, labelled=10)
    v, _ = build_verdicts(golden, [])
    q = compute_quality(results, v, "cand", "ref", n_boot=50)
    assert q.n_changed == 20 and q.n_changed_reviewed == 10
    assert q.improvement is None and q.reference_error_rate is None
    assert q.unreviewed_rows[0] == "r0010"


def test_reference_error_rate_needs_reviewed_same_rows():
    _config, results, golden = _world(10, 2, fixes=2, labelled=0)
    lines = [
        Approval(row_id=r, outcome=o, approved=o == GOOD)
        for r in ("r0000", "r0001")
        for o in (GOOD, BAD)
    ]
    v, _ = build_verdicts(golden, lines)
    q = compute_quality(results, v, "cand", "ref", required_relative=0.1, n_boot=50)
    assert q.improvement.estimate == pytest.approx(0.2)
    assert q.reference_error_rate is None and q.relative_improvement is None and q.margin is None


def test_a_perfect_reference_has_no_relative_improvement():
    _config, results, golden = _world(10, 0)
    v, _ = build_verdicts(golden, [])
    q = compute_quality(results, v, "cand", "ref", required_relative=0.1, n_boot=50)
    assert q.reference_error_rate.estimate == 0 and q.relative_improvement is None
    assert q.improvement.estimate == 0 and q.margin.estimate == 0


# --------------------------------------------------------------------------- the gate


def test_four_percent_more_cost_for_clearly_more_than_four_percent_better_passes():
    config, results, golden = _world(
        200, 50, fixes=20, cand_cost=0.00104, ci={"quality_per_cost": 1.0}
    )
    report, cand = _gate(config, results, golden)
    q = report.candidate("cand").quality
    assert q.required_relative == pytest.approx(0.04)
    assert q.relative_improvement.estimate == pytest.approx(0.4)
    assert cand.passed, cand.reasons


def test_ten_percent_more_cost_for_five_percent_better_fails():
    config, results, golden = _world(200, 80, fixes=4, cand_cost=0.0011, ci={"quality_per_cost": 1})
    report, cand = _gate(config, results, golden)
    assert report.candidate("cand").quality.relative_improvement.estimate == pytest.approx(0.05)
    assert not cand.passed
    (reason,) = cand.reasons
    assert "costs 1.10x the reference, so it must make at least 10.0% fewer errors" in reason
    assert "measured error reduction +5.0%" in reason and "not shown at 95%" in reason


def test_a_point_estimate_that_clears_the_bar_is_not_enough():
    # +50% better on paper, from one fix in 10 rows: the interval reaches 0.
    config, results, golden = _world(10, 2, fixes=1, cand_cost=0.00104, ci={"quality_per_cost": 1})
    report, cand = _gate(config, results, golden)
    q = report.candidate("cand").quality
    assert q.relative_improvement.estimate == pytest.approx(0.5)
    assert q.improvement.low <= 0
    assert not cand.passed and "not shown at 95%" in cand.reasons[0]


def test_unreviewed_changes_and_unknown_base_rate_fail_with_a_next_step():
    config, results, golden = _world(
        20, 0, breaks=20, labelled=10, cand_cost=0.002, ci={"quality_per_cost": 1}
    )
    _, cand = _gate(config, results, golden)
    assert "10 of 20 changed rows have no human verdict on both outcomes" in cand.reasons[0]
    assert "caliper review" in cand.reasons[0]

    config, results, golden = _world(
        10, 2, fixes=2, labelled=0, cand_cost=0.002, ci={"quality_per_cost": 1}
    )
    lines = [
        Approval(row_id=r, outcome=o, approved=o == GOOD)
        for r in ("r0000", "r0001")
        for o in (GOOD, BAD)
    ]
    _, cand = _gate(config, results, golden, lines)
    assert "ref's error rate is unknown" in cand.reasons[0]
    assert "--base-sample" in cand.reasons[0]


def test_paying_more_for_the_same_decisions_fails_even_against_a_perfect_reference():
    config, results, golden = _world(50, 0, cand_cost=0.002, ci={"quality_per_cost": 1})
    _, cand = _gate(config, results, golden)
    assert not cand.passed and "not shown at 95%" in cand.reasons[0]


def test_cheaper_candidates_are_not_judged_by_this_rule():
    config, results, golden = _world(50, 5, breaks=10, cand_cost=0.0005, ci={"quality_per_cost": 1})
    _, cand = _gate(config, results, golden)
    assert not any("fewer errors" in r for r in cand.reasons)


def test_max_cost_ratio_stays_a_hard_ceiling():
    config, results, golden = _world(
        200, 50, fixes=40, cand_cost=0.003, ci={"quality_per_cost": 1, "max_cost_ratio": 1.5}
    )
    _, cand = _gate(config, results, golden)
    assert any("cost 3.00x the reference > allowed 1.5x" in r for r in cand.reasons)


def test_an_unpriced_candidate_cannot_buy_quality():
    config, results, golden = _world(20, 5, fixes=5, cand_cost=0.0, ci={"quality_per_cost": 1})
    _, cand = _gate(config, results, golden)
    assert any(r.startswith("cost unknown") for r in cand.reasons)


def test_config_requires_quality_section_and_rejects_typos():
    base = {
        "golden": "g.jsonl",
        "candidates": [{"name": "a", "provider": "fake", "model": "a"}],
    }
    with pytest.raises(ValueError, match=r"quality_per_cost needs a \[quality\] section"):
        config_from_dict({**base, "ci": {"quality_per_cost": 1}})
    with pytest.raises(ValueError, match="approval"):
        config_from_dict({**base, "quality": {"approval": ["a.jsonl"]}})
    with pytest.raises(ValueError):
        config_from_dict({**base, "ci": {"quality_per_cost": 0}, "quality": {}})


def test_report_and_summary_show_the_comparison():
    config, results, golden = _world(
        200, 50, fixes=20, cand_cost=0.00104, ci={"quality_per_cost": 1.0}
    )
    report = build_report(config, results, golden, n_boot=200)
    gate = evaluate_gate(report)
    text = render_text(report, gate)
    assert "quality vs human approvals (0 approval lines, 200 human golden labels):" in text
    assert "cand: 20 changed rows: 20 fixed, 0 broken" in text
    assert "errors ref 25.0%, cand 15.0%" in text
    assert "cuts errors by at least 1x its extra cost" in text
    md = render_markdown(report, gate)
    assert "**Quality vs human approvals" in md and "- `cand`: 20 changed rows" in md
    assert json.loads(report.model_dump_json())["candidates"][1]["quality"]["fixed"] == 20


def test_no_quality_section_changes_nothing():
    config, results, golden = _world(20, 5, fixes=5)
    config = config.model_copy(update={"quality": None})
    report = build_report(config, results, golden, n_boot=50)
    assert report.approvals is None and report.candidate("cand").quality is None
    assert report.candidate("cand").paired_excess.n_approved_changes == 0
    assert "quality vs human approvals" not in render_text(report)


# --------------------------------------------------------------------------- paired excess


def test_a_change_to_an_approved_outcome_is_not_a_regression():
    config, results, golden = _world(20, 5, fixes=5, breaks=3)
    plain = build_report(config.model_copy(update={"quality": None}), results, golden, n_boot=50)
    judged = build_report(config, results, golden, n_boot=50)
    p0, p1 = plain.candidate("cand").paired_excess, judged.candidate("cand").paired_excess
    assert p0.regression == pytest.approx(8 / 20)  # every change costs 1 by default
    assert p1.regression == pytest.approx(3 / 20)  # the 5 fixes cost nothing
    assert p1.n_approved_changes == 5


# --------------------------------------------------------------------------- ledger


def test_new_approvals_are_a_new_measurement_not_a_retouch(tmp_path: Path):
    config, _results, golden = _world(10, 2, fixes=2)
    ledger = tmp_path / "ledger.jsonl"
    cands = config.candidates
    assert record_touches(ledger, cands, golden, "ref", approvals_key="a") == []
    assert [
        r.candidate for r in record_touches(ledger, cands, golden, "ref", approvals_key="a")
    ] == ["cand"]
    assert record_touches(ledger, cands, golden, "ref", approvals_key="b") == []


def test_approval_fingerprint_ignores_val_rows():
    golden = [
        GoldenRow(id="t", request=_req("t"), split=Split.TEST),
        GoldenRow(id="v", request=_req("v"), split=Split.VAL),
    ]
    _, base = build_verdicts(golden, [])
    _, val = build_verdicts(golden, [Approval(row_id="v", outcome="x", approved=True)])
    _, test = build_verdicts(golden, [Approval(row_id="t", outcome="x", approved=True)])
    assert base.fingerprint == val.fingerprint != test.fingerprint


# --------------------------------------------------------------------------- caliper review


def test_review_lists_unjudged_outcomes_of_changed_rows_and_a_base_sample():
    _config, results, golden = _world(40, 6, fixes=3, breaks=2, labelled=0)
    v, _ = build_verdicts(golden, [Approval(row_id="r0000", outcome=GOOD, approved=True)])
    plan = plan_review(golden, results, v, "ref", ["cand"], base_sample=10, seed=3)
    keys = [(i.row_id, i.outcome) for i in plan.items]
    assert len(keys) == len(set(keys))
    changed = {"r0000", "r0001", "r0002", "r0006", "r0007"}
    # Both outcomes of every changed row, minus the one already judged.
    assert plan.from_changes == 2 * len(changed) - 1 and plan.already_judged == 1
    assert ("r0000", GOOD) not in keys and ("r0000", BAD) in keys
    # The base sample adds the reference's outcome on other rows.
    assert 0 < plan.from_base_sample <= 10
    ref_outcome = {f"r{i:04d}": BAD if i < 6 else GOOD for i in range(40)}
    assert all(o == ref_outcome[r] for r, o in keys if r not in changed)
    assert keys != sorted(keys)  # shuffled
    item = plan.items[0]
    assert item.request == _req(item.row_id)
    assert item.request_sha256 == request_sha256(_req(item.row_id))
    again = plan_review(golden, results, v, "ref", ["cand"], base_sample=10, seed=3)
    assert again == plan


def test_review_round_trip_through_caliper_label(tmp_path: Path, capsys):
    pytest.importorskip("caliper_label")
    from caliper_label.app import create_app
    from caliper_label.context import ProjectContext
    from fastapi.testclient import TestClient

    _config, results, golden = _world(30, 6, fixes=4, breaks=1, labelled=0)
    work = tmp_path / "eval"
    work.mkdir()
    (tmp_path / "golden.jsonl").write_text(
        "".join(g.model_dump_json(exclude_none=True) + "\n" for g in golden)
    )
    (work / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in results))
    cfg = tmp_path / "caliper.toml"
    cfg.write_text(
        'golden = "golden.jsonl"\nreference = "ref"\n'
        '[[candidates]]\nname = "ref"\nprovider = "fake"\nmodel = "ref"\n'
        '[[candidates]]\nname = "cand"\nprovider = "fake"\nmodel = "cand"\n'
        "[quality]\napprovals = []\n"
    )
    review = tmp_path / "review"
    assert (
        main(
            [
                "review",
                "--config",
                str(cfg),
                "--eval-out",
                str(work),
                "--out",
                str(review),
                "--base-sample",
                "8",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "outcomes to review: 10 from rows where a candidate and ref decide differently" in out
    assert f"next: caliper label {review}" in out
    assert (
        main(["review", "--config", str(cfg), "--eval-out", str(work), "--out", str(review)]) == 2
    )

    ctx = ProjectContext.open(review)
    client = TestClient(create_app(ctx=ctx))
    shown = set()
    while True:
        body = client.get("/api/next?labeller=ana&n=1").json()
        if body["done"]:
            break
        item = body["item"]
        shown.update(f["label"] for f in item["fields"])
        fields = ctx.store.item(item["id"]).fields
        decision = "yes" if fields["outcome"] == GOOD else "no"
        r = client.post(
            "/api/labels", json={"item_id": item["id"], "labeller": "ana", "decision": decision}
        )
        assert r.status_code == 200, r.text
    assert not {"row_id", "request_sha256"} & {s.lower() for s in shown}  # never on the card

    approvals = review / "approvals.jsonl"
    assert main(["label", "export", str(review), "--format", "jsonl", "--out", str(approvals)]) == 0
    lines = read_approvals(approvals)
    assert len(lines) == len((review / "items.jsonl").read_text().splitlines())
    v, s = build_verdicts(golden, lines)
    assert s.stale == 0 and s.used == len(lines)
    q = compute_quality(results, v, "cand", "ref", n_boot=50)
    assert q.n_changed == q.n_changed_reviewed == 5
    assert (q.fixed, q.broken) == (4, 1)


def test_eval_rejects_a_broken_approvals_file(tmp_path: Path, capsys):
    (tmp_path / "golden.jsonl").write_text(GoldenRow(id="r", request=_req("r")).model_dump_json())
    (tmp_path / "bad.jsonl").write_text("not json\n")
    cfg = tmp_path / "caliper.toml"
    cfg.write_text(
        'golden = "golden.jsonl"\ndecide = "d.py:decide"\n'
        '[[candidates]]\nname = "ref"\nprovider = "fake"\nmodel = "ref"\n'
        '[quality]\napprovals = ["bad.jsonl"]\n'
    )
    (tmp_path / "d.py").write_text(
        "from caliper_eval.types import Decision\n"
        "def decide(replies):\n    return Decision(outcome='x')\n"
    )
    assert main(["eval", "--config", str(cfg), "--out", str(tmp_path / "out")]) == 2
    assert "bad.jsonl:1: invalid approval" in capsys.readouterr().err


def test_bundled_quality_example_matches_its_readme(tmp_path: Path, capsys):
    from caliper_eval.examples import copy_example

    ex = copy_example("catalog_cleanup", tmp_path / "cc")
    out = tmp_path / "out"
    assert (
        main(["eval", "--config", str(ex / "caliper.quality.toml"), "--out", str(out), "--ci"]) == 1
    )
    text = capsys.readouterr().out
    report = json.loads((out / "report.json").read_text())
    assert report["approvals"]["stale"] == 0 and report["approvals"]["used"] == 8
    q = {c["name"]: c["quality"] for c in report["candidates"] if c["quality"]}
    assert all(v["n_changed_reviewed"] == v["n_changed"] for v in q.values())
    assert (q["small-thinking"]["fixed"], q["small-thinking"]["broken"]) == (1, 3)
    assert (q["small"]["fixed"], q["small"]["broken"]) == (5, 15)
    assert "must make at least 3.8% fewer errors" in text
    assert "measured error reduction -8.4% [-32.4%, +6.9%]" in text
    assert "0.150 [0.050, 0.263]" in text  # small's paired excess once approved changes are free


def test_review_without_the_label_extra_still_writes_items(tmp_path: Path, capsys, monkeypatch):
    import importlib.util

    from caliper_eval import review

    _config, results, golden = _world(10, 2, fixes=2, labelled=0)
    (tmp_path / "golden.jsonl").write_text(
        "".join(g.model_dump_json(exclude_none=True) + "\n" for g in golden)
    )
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "results.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results)
    )
    cfg = tmp_path / "caliper.toml"
    cfg.write_text(
        'golden = "golden.jsonl"\nreference = "ref"\n'
        '[[candidates]]\nname = "ref"\nprovider = "fake"\nmodel = "ref"\n'
        '[[candidates]]\nname = "cand"\nprovider = "fake"\nmodel = "cand"\n'
    )
    real = importlib.util.find_spec
    monkeypatch.setattr(
        review.importlib.util, "find_spec", lambda m: None if m == "ruamel.yaml" else real(m)
    )
    rev = tmp_path / "review"
    assert (
        main(
            [
                "review",
                "--config",
                str(cfg),
                "--eval-out",
                str(tmp_path / "eval"),
                "--out",
                str(rev),
            ]
        )
        == 0
    )
    assert "needs the label extra" in capsys.readouterr().out
    assert (rev / "items.jsonl").is_file() and not (rev / "caliper.label.yaml").exists()
