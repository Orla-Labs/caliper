"""Phase 1b analysis: label provenance, splits, slices, matched operating point,
paired bootstrap gate, test ledger."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from caliper_eval._stats import bootstrap_mean_ci, paired_bootstrap_delta
from caliper_eval.calibration import compute_calibration
from caliper_eval.ci import evaluate_gate
from caliper_eval.config import (
    CIConfig,
    GateRequirement,
    OperatingPointConfig,
    SliceConfig,
    config_from_dict,
)
from caliper_eval.ledger import read_ledger, record_touches, split_sha256
from caliper_eval.operating_point import _match_threshold, compute_matched_operating_point
from caliper_eval.provenance import basis_kind, metric_name, source_key, summarize
from caliper_eval.report import build_report, render_markdown, render_text
from caliper_eval.slices import evaluate_slices, in_slice, slice_members
from caliper_eval.transitions import compute_paired_excess
from caliper_eval.types import (
    Candidate,
    Decision,
    GoldenRow,
    LabelSource,
    ProviderKind,
    RowResult,
    request_sha256,
)

# --------------------------------------------------------------------------- builders


def row(i, *, ok=True, source="internal_reviewer", split=None, slices=(), stratum=None):
    return GoldenRow(
        id=f"r{i:02d}",
        request={"messages": [{"role": "user", "content": str(i)}]},
        label_approved=ok,
        label_source=source,
        split=split,
        slices=list(slices),
        stratum=stratum,
    )


def res(cand, i, outcome, score=None, replica=0):
    return RowResult(
        run_id="t",
        candidate=cand,
        row_id=f"r{i:02d}",
        replica=replica,
        request_sha256=request_sha256({"messages": [{"role": "user", "content": str(i)}]}),
        replies=[],
        decision=None if outcome is None else Decision(outcome=outcome, score=score),
    )


def cand(name):
    return Candidate(name=name, provider=ProviderKind.FAKE, model=f"m-{name}")


def cfg(**kw):
    base = dict(
        reference="ref",
        noise_replays=2,
        seed=0,
        costs={},
        candidates=[cand("ref"), cand("new")],
        ci=CIConfig(max_regression=0.1),
        calibration=SimpleNamespace(thresholds=[0.5, 0.7, 0.9]),
        operating_point=OperatingPointConfig(auto_outcomes=["auto"]),
        slices=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- paired bootstrap


def test_paired_delta_is_zero_for_identical_series():
    a = [0.0, 1.0, 0.0, 5.0, 0.0, 1.0]
    ci = paired_bootstrap_delta(a, a, seed=1, n_boot=200)
    assert ci.estimate == ci.low == ci.high == 0.0


def test_paired_ci_is_narrower_than_two_separate_intervals():
    # strongly correlated rows: the candidate is the reference plus a small shift
    a = [float(i % 7) for i in range(60)]
    b = [x + (0.5 if i % 10 == 0 else 0.0) for i, x in enumerate(a)]
    paired = paired_bootstrap_delta(b, a, seed=3, n_boot=400)
    ca = bootstrap_mean_ci(a, seed=3, n_boot=400)
    cb = bootstrap_mean_ci(b, seed=4, n_boot=400)
    assert paired.low > 0.0  # paired: the shift is detected
    assert cb.low < ca.high  # separate intervals overlap: the shift is not
    assert paired.high - paired.low < (cb.high - cb.low)


def test_paired_delta_rejects_unpaired_lengths():
    with pytest.raises(ValueError):
        paired_bootstrap_delta([1.0], [1.0, 2.0], seed=0)


def _replay_fixture():
    """ref replayed twice; new flips rows 0-3 (cost 1); ref itself flips row 9."""
    golden = [row(i) for i in range(20)]
    results = []
    for i in range(20):
        results.append(res("ref", i, "auto", 0.9))
        results.append(res("ref", i, "review" if i == 9 else "auto", 0.9, replica=1))
        results.append(res("new", i, "review" if i < 4 else "auto", 0.4 if i < 4 else 0.9))
    return golden, results


def test_paired_excess_subtracts_per_row_noise():
    _, results = _replay_fixture()
    p = compute_paired_excess(results, "new", "ref", seed=0, n_boot=300)
    assert p.n_rows == 20 and p.n_without_noise == 0
    assert p.regression == 4 / 20 and p.noise == 1 / 20
    assert abs(p.delta.estimate - 3 / 20) < 1e-12
    sub = compute_paired_excess(results, "new", "ref", rows=["r00", "r09"], n_boot=50)
    assert sub.n_rows == 2 and sub.regression == 0.5 and sub.noise == 0.5


def test_gate_requirement_upper_vs_lower_bound():
    golden, results = _replay_fixture()
    rep = build_report(cfg(), results, golden, n_boot=300)
    d = rep.candidate("new").paired_excess.delta
    assert d.low < 0.1 < d.high  # the interesting zone: depends on the requirement
    strict = evaluate_gate(rep)
    assert not strict.passed
    assert any("upper bound" in r for r in strict.reasons)
    rep2 = build_report(
        cfg(ci=CIConfig(max_regression=0.1, require="no_evidence_of_regression")),
        results,
        golden,
        n_boot=300,
    )
    assert rep2.require == GateRequirement.NO_EVIDENCE_OF_REGRESSION
    assert evaluate_gate(rep2).passed
    assert "fail when paired excess lower bound > 0.1" in render_text(rep2, evaluate_gate(rep2))


# --------------------------------------------------------------------------- provenance


def test_metric_names_follow_the_label_source():
    assert metric_name(["internal_reviewer"]) == "precision"
    assert metric_name(["human_end_user", "internal_reviewer"]) == "precision"
    assert metric_name(["llm_teacher"]) == "teacher agreement"
    assert metric_name(["heuristic"]) == "heuristic agreement"
    assert metric_name(["unknown"]) == "agreement (unknown source)"
    assert metric_name(["internal_reviewer", "llm_teacher"]) == "label agreement"
    assert basis_kind(["llm_teacher"]) == "teacher_agreement"
    assert source_key(row(0, source=None)) == "unknown"


def _split_fixture():
    """val: human rows pick the threshold; test: human + teacher + unknown rows."""
    golden, results = [], []
    # val: 10 human rows; scores 0.55/0.75 are unapproved, 0.95 approved
    for i in range(10):
        s = [0.55, 0.75, 0.95, 0.95, 0.95][i % 5]
        golden.append(row(i, ok=s > 0.9, split="val"))
        results.append(res("ref", i, "auto" if s >= 0.7 else "review", s))
    # val: teacher rows that would pull the threshold down if they were used
    for i in range(10, 14):
        golden.append(row(i, ok=True, source="llm_teacher", split="val"))
        results.append(res("ref", i, "review", 0.55))
    # test: human, teacher, unknown
    for i, (src, ok, s) in enumerate(
        [("internal_reviewer", True, 0.95), ("internal_reviewer", False, 0.92),
         ("llm_teacher", True, 0.95), ("llm_teacher", True, 0.6), (None, True, 0.95)],
        start=20,
    ):  # fmt: skip
        golden.append(row(i, ok=ok, source=src, split="test"))
        results.append(res("ref", i, "auto" if s >= 0.7 else "review", s))
    return golden, results


def test_calibration_picks_on_val_human_rows_and_reports_per_source_on_test():
    golden, results = _split_fixture()
    cal = compute_calibration(golden, results, "ref", thresholds=[0.5, 0.7, 0.9],
                              target_precision=0.95)  # fmt: skip
    assert cal.selection_split == "val" and cal.report_split == "test"
    assert cal.basis == "human_truth" and cal.metric == "precision"
    assert cal.basis_sources == ["internal_reviewer"] and cal.n_rows == 10
    assert cal.recommended_threshold == 0.9  # teacher val rows were not used
    by = {m.source: m for m in cal.on_report}
    assert list(by) == ["internal_reviewer", "llm_teacher", "unknown"]  # human first
    assert by["internal_reviewer"].metric == "precision"
    assert by["internal_reviewer"].auto_approved_rate == 0.5  # optimistic on val, not on test
    assert by["llm_teacher"].metric == "teacher agreement"
    assert by["llm_teacher"].kind == "teacher_agreement"
    assert by["unknown"].metric == "agreement (unknown source)"
    assert list(cal.sweep_by_source) == ["internal_reviewer", "llm_teacher"]  # val sources
    assert cal.sweep_by_source["llm_teacher"][0].n_auto == 4  # teacher rows kept apart


def test_calibration_without_splits_or_sources_keeps_old_numbers():
    golden = [row(i, ok=i % 2 == 0, source=None) for i in range(6)]
    results = [res("c", i, "x", 0.9 if i % 2 == 0 else 0.4) for i in range(6)]
    cal = compute_calibration(golden, results, "c", thresholds=[0.5], target_precision=0.9)
    assert cal.selection_split == cal.report_split == "all"
    assert cal.metric == "agreement (unknown source)"
    assert cal.sweep[0].auto_approved_rate == 1.0 and cal.recommended_threshold == 0.5


def test_teacher_numbers_are_never_called_precision_or_accuracy():
    golden, results = _split_fixture()
    for g in golden:
        g.label_source = "llm_teacher"  # no human truth at all
    results += [res("new", int(r.row_id[1:]), r.decision.outcome, r.decision.score)
                for r in results]  # fmt: skip
    rep = build_report(cfg(), results, golden, n_boot=50, target_precision=0.9)
    assert not rep.provenance.has_human_truth
    text = render_text(rep, evaluate_gate(rep))
    md = render_markdown(rep, evaluate_gate(rep))
    assert "NO HUMAN-TRUTH LABELS" in text and "NO HUMAN-TRUTH LABELS" in md
    assert "teacher agreement" in text
    for blob in (text, md, rep.model_dump_json()):
        low = blob.lower().replace("target_precision", "").replace("not precision", "")
        assert "accuracy" not in low
        assert "precision" not in low.replace("min_precision", "")


def test_provenance_summary_counts_unknown_sources():
    golden, _ = _split_fixture()
    p = summarize(golden)
    assert p.by_source == {"internal_reviewer": 12, "llm_teacher": 6, "unknown": 1}
    assert p.has_human_truth and p.has_splits and p.by_split == {"test": 5, "val": 14}


# --------------------------------------------------------------------------- operating point


def test_match_threshold_closest_coverage_ties_go_higher_and_none_never_auto():
    assert _match_threshold([0.9, 0.8, 0.7, 0.6], 0.5) == 0.8
    assert _match_threshold([0.9, 0.9, 0.5, 0.5], 0.75) == 0.9  # 50% vs 100%: tie -> higher
    assert _match_threshold([None, 0.9, 0.8, None], 0.5) == 0.8
    assert _match_threshold([None, None], 0.5) is None


def _mop_fixture():
    golden, results = [], []
    # val: ref auto-handles 2 of 4; candidate scores rank the same rows
    for i, (ro, cs) in enumerate([("auto", 0.9), ("auto", 0.8), ("review", 0.3), ("review", 0.2)]):
        golden.append(row(i, split="val"))
        results += [res("ref", i, ro, 0.9), res("new", i, "x", cs)]
    # test: (approved, ref outcome, candidate score)
    test = [
        (True, "auto", 0.85),   # both right
        (True, "auto", 0.30),   # candidate misses (lost vs ref)
        (False, "review", 0.95),  # candidate bad auto (lost vs ref)
        (False, "auto", 0.10),  # ref bad auto; candidate right
        (True, "review", 0.20),  # both miss
    ]
    for j, (ok, ro, cs) in enumerate(test, start=10):
        golden.append(row(j, ok=ok, split="test", slices=["must"] if j in (11, 12) else []))
        results += [res("ref", j, ro, 0.9), res("new", j, "x", cs)]
    return golden, results


def test_matched_operating_point_counts_losses_on_test():
    golden, results = _mop_fixture()
    m = compute_matched_operating_point(golden, results, "new", "ref",
                                        auto_outcomes=["auto"], n_boot=100)  # fmt: skip
    assert m.selection_split == "val" and m.report_split == "test"
    assert m.reference_coverage == 0.5 and m.threshold == 0.8 and m.candidate_coverage == 0.5
    (s,) = m.by_source
    assert s.kind == "human_truth" and s.n_rows == 5
    assert (s.reference.n_auto, s.reference.missed, s.reference.bad_auto) == (3, 1, 1)
    assert (s.candidate.n_auto, s.candidate.missed, s.candidate.bad_auto) == (2, 2, 1)
    assert s.candidate_only == ["r11", "r12"] and s.reference_only == ["r13"]
    assert abs(s.delta.estimate - (3 - 2) / 5) < 1e-12


def test_matched_operating_point_without_scores_uses_own_outcomes():
    golden, results = _mop_fixture()
    results = [r.model_copy(update={"decision": Decision(outcome="auto")})
               if r.candidate == "new" else r for r in results]  # fmt: skip
    m = compute_matched_operating_point(golden, results, "new", "ref", auto_outcomes=["auto"])
    assert m.threshold is None and m.candidate_coverage == 1.0


def test_matched_operating_point_needs_labelled_report_rows():
    golden, results = _mop_fixture()
    for g in golden:
        g.label_approved = None
    assert compute_matched_operating_point(golden, results, "new", "ref",
                                           auto_outcomes=["auto"]) is None  # fmt: skip


# --------------------------------------------------------------------------- slices


def test_slice_membership_by_tag_or_where():
    tagged = row(1, slices=["lookalike"])
    by_stratum = row(2, stratum="lookalike")
    meta = row(3).model_copy(update={"metadata": {"family": {"code": "CTX-520"}}})
    s1 = SliceConfig(name="lookalike", rule="max_regression", threshold=0.1)
    s2 = SliceConfig(name="look", rule="max_regression", threshold=0.1,
                     where={"stratum": ["lookalike", "mixed"]})  # fmt: skip
    s3 = SliceConfig(name="rare", rule="max_regression", threshold=0.1,
                     where={"metadata.family.code": "CTX-520"})  # fmt: skip
    assert in_slice(tagged, s1) and not in_slice(by_stratum, s1)
    assert in_slice(by_stratum, s2) and not in_slice(tagged, s2)
    assert [g.id for g in slice_members([tagged, by_stratum, meta], s3)] == ["r03"]


def test_must_not_lose_fails_on_any_lost_row_and_passes_when_none():
    golden, results = _mop_fixture()
    m = compute_matched_operating_point(golden, results, "new", "ref", auto_outcomes=["auto"])
    must = SliceConfig(name="must", rule="must_not_lose")
    (r,) = evaluate_slices([must], golden, results, "new", "ref", mop=m)
    assert not r.passed and r.value == 2 and r.rows == ["r11", "r12"]
    assert (r.reference_lost, r.candidate_lost) == (0, 2)
    assert r.detail == (
        "2 human-labelled rows on test: ref loses 0, new loses 2; 2 lost vs ref: r11, r12"
    )
    # teacher verdicts are not truth: ignored by default, "disagreements" when asked for
    for g in golden:
        if g.id == "r11":
            g.label_source = LabelSource.LLM_TEACHER
    (rh,) = evaluate_slices([must], golden, results, "new", "ref", mop=m)
    assert rh.rows == ["r12"]
    teach = SliceConfig(name="must", rule="must_not_lose", label_sources=["llm_teacher"])
    (rt,) = evaluate_slices([teach], golden, results, "new", "ref", mop=m)
    assert not rt.passed and rt.rows == ["r11"]
    assert "teacher agreement" in rt.metric and "disagreements vs ref" in rt.detail
    assert "loses" not in rt.detail and "precision" not in rt.detail
    other = SliceConfig(name="other", rule="must_not_lose", where={"stratum": "none"})
    (r2,) = evaluate_slices([other], golden, results, "new", "ref", mop=m)
    assert not r2.passed and "cannot show nothing is lost" in r2.detail  # fails closed
    golden[-1].slices = ["safe"]
    safe = SliceConfig(name="safe", rule="must_not_lose")
    (r3,) = evaluate_slices([safe], golden, results, "new", "ref", mop=m)
    assert r3.passed and r3.value == 0


def test_min_precision_reads_human_rows_only_and_names_teacher_agreement():
    golden, results = _mop_fixture()
    for g in golden:
        g.slices = ["s"]
    results = [r.model_copy(update={"decision": Decision(outcome="auto", score=0.9)})
               if r.candidate == "new" else r for r in results]  # fmt: skip
    rule = SliceConfig(name="s", rule="min_precision", threshold=0.6)
    (r,) = evaluate_slices([rule], golden, results, "new", "ref", auto_outcomes=["auto"])
    assert r.metric == "precision" and r.value == 3 / 5 and r.passed
    for g in golden:
        g.label_source = "llm_teacher"
    (r2,) = evaluate_slices([rule], golden, results, "new", "ref", auto_outcomes=["auto"])
    assert not r2.passed and r2.value is None and "human-labelled" in r2.detail
    teach = SliceConfig(name="s", rule="min_precision", threshold=0.6,
                        label_sources=["llm_teacher"])  # fmt: skip
    (r3,) = evaluate_slices([teach], golden, results, "new", "ref", auto_outcomes=["auto"])
    assert r3.metric == "teacher agreement" and "precision" not in r3.detail


def test_slice_max_regression_and_gate_reasons():
    golden, results = _replay_fixture()
    for g in golden[:4]:
        g.slices = ["hot"]
    for g in golden[4:8]:
        g.slices = ["cold"]
    slices = [
        SliceConfig(name="hot", rule="max_regression", threshold=0.2),
        SliceConfig(name="cold", rule="max_regression", threshold=0.2),
    ]
    rep = build_report(cfg(slices=slices, ci=CIConfig(max_regression=5.0)), results, golden,
                       n_boot=200)  # fmt: skip
    by = {s.slice: s for s in rep.candidate("new").slices}
    assert not by["hot"].passed and by["hot"].rows == ["r00", "r01", "r02", "r03"]
    assert by["cold"].passed and by["cold"].value == 0.0
    gate = evaluate_gate(rep)
    assert gate.reasons == [
        f"new: slice hot (max_regression): {by['hot'].detail}",
    ]
    text = render_text(rep, gate)
    assert "slices:" in text and "hot (max_regression <= 0.2; 4 rows)" in text


@pytest.mark.parametrize(
    ("extra", "msg"),
    [
        ({"slices": [{"name": "a", "rule": "max_regression"}]}, "needs a threshold"),
        ({"slices": [{"name": "a", "rule": "must_not_lose"}]}, "need [operating_point]"),
        ({"slices": [{"name": "a", "rule": "must_not_lose", "threshold": 1}],
          "operating_point": {"auto_outcomes": ["x"]}}, "takes no threshold"),
        ({"slices": [{"name": "a", "rule": "max_regression", "threshold": 1,
                      "where": {"label": "x"}}]}, "must be 'stratum'"),
        ({"slices": [{"name": "a", "rule": "max_regression", "threshold": 1}] * 2},
         "duplicate slice names"),
        ({"slices": [{"name": "a", "rule": "min_precision", "threshold": 2.0}],
          "operating_point": {"auto_outcomes": ["x"]}}, "must be in [0, 1]"),
        ({"ci": {"require": "vibes"}}, "require"),
    ],
)  # fmt: skip
def test_config_validation(extra, msg):
    data = {
        "golden": "g.jsonl",
        "candidates": [{"name": "a", "provider": "fake", "model": "m"}],
        **extra,
    }
    with pytest.raises(ValueError, match=msg.replace("[", r"\[").replace("]", r"\]")):
        config_from_dict(data)


# --------------------------------------------------------------------------- test ledger


def test_ledger_records_touches_and_flags_retouch(tmp_path):
    golden = [row(i, split="test" if i % 2 else "val") for i in range(6)]
    cands = [cand("ref"), cand("new")]
    path = tmp_path / "ledger.jsonl"
    assert record_touches(path, cands, golden, "ref", clock=lambda: "T1") == []
    again = record_touches(path, cands, golden, "ref", clock=lambda: "T2")
    assert [(r.candidate, r.previous_touches) for r in again] == [("new", 1)]  # ref exempt
    touches = read_ledger(path)
    assert [(t.candidate, t.reference, t.touched_at) for t in touches] == [
        ("ref", True, "T1"), ("new", False, "T1"), ("ref", True, "T2"), ("new", False, "T2"),
    ]  # fmt: skip
    assert all(t.n_test_rows == 3 for t in touches)
    # a changed candidate is a first touch
    changed = [cand("ref"), cand("new").model_copy(update={"model": "m-other"})]
    assert record_touches(path, changed, golden, "ref") == []


def test_test_set_hash_ignores_val_changes_and_tracks_test_changes():
    golden = [row(i, split="test" if i % 2 else "val") for i in range(6)]
    h = split_sha256(golden)
    assert split_sha256([*golden, row(40, split="val")]) == h
    edited = [g.model_copy(update={"label_approved": False}) if g.id == "r01" else g
              for g in golden]  # fmt: skip
    assert split_sha256(edited) != h
    assert split_sha256([row(0, split="val")]) is None
    assert split_sha256([row(0)]) is None


def test_no_test_rows_records_nothing(tmp_path):
    path = tmp_path / "l.jsonl"
    assert record_touches(path, [cand("a")], [row(0)], "a") == []
    assert not path.exists()


def test_gate_fails_retouched_candidates_only():
    golden, results = _replay_fixture()
    rep = build_report(cfg(ci=CIConfig(max_regression=5.0)), results, golden, n_boot=50)
    assert evaluate_gate(rep).passed
    gate = evaluate_gate(rep, retouched={"new": 2})
    assert not gate.passed
    assert gate.reasons == [
        "new: test split already reported for these settings (2 earlier touches); "
        "pass --allow-test-retouch to accept"
    ]


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def split_project(project, monkeypatch):
    """The conftest project with a val/test split on its golden rows."""
    monkeypatch.chdir(project)
    path = project / "golden.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    for i, r in enumerate(rows):
        r["split"] = "test" if i % 2 else "val"
        r["label_source"] = "internal_reviewer"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg_text = (project / "eval.toml").read_text()
    (project / "eval.toml").write_text(
        cfg_text.replace("max_regression = 0.02", "max_regression = 10.0")
    )
    return project


def test_cli_retouch_fails_ci_unless_allowed(split_project, capsys):
    from caliper_eval.cli import main

    base = ["eval", "--config", "eval.toml", "--ci", "--n-boot", "50"]
    assert main(base) == 0
    out1, err1 = capsys.readouterr()
    ledger = read_ledger(split_project / "caliper-out" / "test_ledger.jsonl")
    assert [t.candidate for t in ledger] == ["baseline", "small"]
    assert ledger[0].touched_at not in out1 and "re-touched" not in err1

    assert main(base) == 1
    out2, err2 = capsys.readouterr()
    assert "small: test split already reported" in out2
    assert "test split re-touched: small" in err2 and "fails the gate" in err2

    assert main([*base, "--allow-test-retouch"]) == 0
    out3, err3 = capsys.readouterr()
    assert out3 == out1  # stdout unchanged by the ledger
    assert "accepted (--allow-test-retouch)" in err3
    assert len(read_ledger(split_project / "caliper-out" / "test_ledger.jsonl")) == 6

    custom = split_project / "shared" / "ledger.jsonl"
    assert main([*base, "--no-resume", "--test-ledger", str(custom)]) == 0
    assert len(read_ledger(custom)) == 2
