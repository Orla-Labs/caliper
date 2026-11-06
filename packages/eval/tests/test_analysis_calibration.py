import pytest
from caliper_eval.calibration import DEFAULT_THRESHOLDS, compute_calibration
from caliper_eval.types import Decision, GoldenRow, Reply, RowResult


def rr(row, score, cand="c", outcome="high"):
    return RowResult(
        run_id="t",
        candidate=cand,
        row_id=row,
        request_sha256="x",
        replies=[Reply(text="x")],
        decision=None if score is None else Decision(outcome=outcome, score=score),
        error=None if score is not None else "boom",
    )


def g(row, approved):
    return GoldenRow(id=row, request={"messages": []}, label_approved=approved)


# 10 rows: scores 0.05..0.95; approved iff score >= 0.5, except row 9 (0.95) rejected
SCORES = [round(0.05 + 0.1 * i, 2) for i in range(10)]
GOLDEN = [g(f"r{i}", s >= 0.5 and i != 9) for i, s in enumerate(SCORES)]
RESULTS = [rr(f"r{i}", s) for i, s in enumerate(SCORES)]


def test_default_grid_is_clean():
    assert DEFAULT_THRESHOLDS[0] == 0.05 and DEFAULT_THRESHOLDS[-1] == 0.95
    assert 0.3 in DEFAULT_THRESHOLDS and len(DEFAULT_THRESHOLDS) == 19


def test_sweep_metrics():
    cal = compute_calibration(GOLDEN, RESULTS, "c", thresholds=[0.5, 0.9, 0.99])
    assert cal.n_rows == 10 and cal.n_approved == 4
    p = {pt.threshold: pt for pt in cal.sweep}
    assert p[0.5].n_auto == 5 and p[0.5].coverage == 0.5
    assert p[0.5].precision == 4 / 5
    assert p[0.5].approved_to_review == 0.0
    assert p[0.9].n_auto == 1 and p[0.9].precision == 0.0
    assert p[0.9].approved_to_review == 1.0
    assert p[0.99].precision is None and p[0.99].n_auto == 0
    assert p[0.99].kappa == 0.0  # constant rule: no agreement beyond chance
    # 2x2 at t=0.5: auto&ok=4, auto&!ok=1, rev&ok=0, rev&!ok=5 -> po=.9, pe=.5 -> kappa .8
    assert p[0.5].kappa == pytest.approx(0.8)


def test_recommendation_lowest_meeting_target():
    cal = compute_calibration(GOLDEN, RESULTS, "c", target_precision=0.75)
    assert cal.recommended_threshold == 0.5
    pts = {p.threshold: p for p in cal.sweep}
    assert pts[0.45].precision == 4 / 6 < 0.75 <= pts[0.5].precision
    strict = compute_calibration(GOLDEN, RESULTS, "c", target_precision=0.99)
    assert strict.recommended_threshold is None


def test_reliability_and_ece():
    cal = compute_calibration(GOLDEN, RESULTS, "c")
    assert len(cal.reliability) == 10
    assert sum(b.n for b in cal.reliability) == 10
    assert cal.reliability[9].n == 1 and cal.reliability[9].approved_rate == 0.0
    assert 0.0 < cal.ece < 1.0


def test_exclusions_counted():
    golden = [*GOLDEN, g("x1", True), g("x2", False), GoldenRow(id="u", request={})]
    results = [*RESULTS, rr("x1", None), rr("x2", 1.5)]
    cal = compute_calibration(golden, results, "c")
    assert cal.n_no_score == 1
    assert cal.n_out_of_range == 1
    assert cal.n_unlabeled == 1
    assert cal.n_rows == 10


def test_none_without_labels():
    assert compute_calibration([GoldenRow(id="a", request={})], RESULTS, "c") is None
