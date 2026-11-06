from types import SimpleNamespace

from caliper_eval.ci import evaluate_gate
from caliper_eval.report import build_report, render_markdown, render_text
from caliper_eval.types import (
    Candidate,
    Decision,
    GoldenRow,
    ProviderKind,
    Reply,
    RowResult,
    ServedBy,
    Usage,
    request_sha256,
)

N = 30


def _cand(name, model):
    return Candidate(name=name, provider=ProviderKind.FAKE, model=model)


def _reply(model, *, i, mismatch=False, error=None):
    return Reply(
        text="x",
        served_by=None
        if error
        else ServedBy(
            provider=ProviderKind.FAKE,
            model="fake-other" if mismatch else model,
            endpoint="fake",
            matches_request=not mismatch,
        ),
        usage=Usage(input_tokens=100, output_tokens=10, cache_read_tokens=50),
        latency_ms=float(100 + (i * 7) % 50),
        cost_usd=0.0002,
        error=error,
    )


def _rr(cand, model, i, outcome, score, replica=0, **kw):
    err = kw.get("error")
    return RowResult(
        run_id="t",
        candidate=cand,
        row_id=f"sku-{i:03d}",
        replica=replica,
        request_sha256=request_sha256({"messages": []}),
        replies=[_reply(model, i=i, **kw)],
        decision=None if err else Decision(outcome=outcome, score=score),
        error=err,
    )


def truth(i):
    return "high" if i % 3 else "low"


def fixture():
    cands = [
        _cand("baseline", "fake-large"),
        _cand("small", "fake-small"),
        _cand("swapped", "fake-medium"),
    ]
    golden = [
        GoldenRow(id=f"sku-{i:03d}", request={"messages": []}, label_approved=bool(i % 3))
        for i in range(N)
    ]
    res = []
    for i in range(N):
        s = 0.9 if i % 3 else 0.3
        res.append(_rr("baseline", "fake-large", i, truth(i), s, 0))
        # one replay flip on row 4 (high -> low)
        res.append(_rr("baseline", "fake-large", i, "low" if i == 4 else truth(i), s, 1))
        # small drops many highs
        small = "low" if i % 3 == 1 else truth(i)
        res.append(_rr("small", "fake-small", i, small, 0.4 if small == "low" else 0.8))
        # swapped matches outcomes but 2 replies came from another model, 1 errored
        if i == 7:
            res.append(_rr("swapped", "fake-medium", i, None, None, error="timeout"))
        else:
            res.append(_rr("swapped", "fake-medium", i, truth(i), s, mismatch=i in (2, 5)))
    config = SimpleNamespace(
        reference="baseline",
        noise_replays=2,
        seed=0,
        costs={"high->low": 2.0, "low->high": 1.0},
        candidates=cands,
        ci=SimpleNamespace(max_regression=0.02),
        calibration=SimpleNamespace(thresholds=[0.5, 0.85]),
    )
    return config, res, golden


def test_build_report_aggregates():
    config, res, golden = fixture()
    rep = build_report(config, res, golden, n_boot=200, target_precision=0.9)
    assert [c.name for c in rep.candidates] == ["baseline", "small", "swapped"]
    base = rep.candidate("baseline")
    assert base.is_reference and base.transitions is None
    assert base.results == 2 * N and base.rows == N
    assert base.tokens.input == 100 * 2 * N
    assert round(base.dollars, 6) == round(0.0002 * 2 * N, 6)
    assert rep.noise_floor.flipped_rows == ["sku-004"]
    # symmetrized (2 + 1) / 2 / 30
    assert rep.noise_floor.cost_weighted_flip_rate.estimate == 1.5 / N
    small = rep.candidate("small")
    assert small.transitions.regression.estimate == 2.0 * 10 / N
    # paired excess: regression minus the per-row replay noise, on the same rows
    pe = small.paired_excess
    assert pe.n_rows == N and pe.n_without_noise == 0
    assert pe.regression == small.transitions.regression.estimate
    assert pe.noise == rep.noise_floor.cost_weighted_flip_rate.estimate
    assert abs(pe.delta.estimate - (pe.regression - pe.noise)) < 1e-12
    assert pe.delta.low <= pe.delta.estimate <= pe.delta.high
    sw = rep.candidate("swapped")
    assert sw.served_mismatches == 2
    assert sw.served_models == ["fake:fake-medium", "fake:fake-other"]
    assert sw.decide_errors == 1 and sw.provider_errors == 1
    assert sw.transitions.n_errored == 1
    assert base.calibration.recommended_threshold == 0.5
    assert 100 <= base.latency_p50_ms <= base.latency_p95_ms < 150


def test_gate():
    config, res, golden = fixture()
    rep = build_report(config, res, golden, n_boot=200)
    gate = evaluate_gate(rep)
    assert not gate.passed and gate.exit_code == 1
    by = {g.name: g for g in gate.candidates}
    assert by["baseline"].passed
    assert any("regression" in r for r in by["small"].reasons)
    assert any("served by a model other" in r for r in by["swapped"].reasons)
    assert any("error rate" in r for r in by["swapped"].reasons)
    assert not any("regression" in r for r in by["swapped"].reasons)
    lenient = evaluate_gate(rep, max_regression=1.0, max_error_rate=0.5)
    assert {g.name for g in lenient.candidates if not g.passed} == {"swapped"}


def test_gate_passes_clean_candidate():
    config, res, golden = fixture()
    config.candidates = [*config.candidates[:1], _cand("twin", "fake-large")]
    res = [r for r in res if r.candidate == "baseline"]
    res += [
        r.model_copy(update={"candidate": "twin"}) for r in res if r.replica == 0
    ]
    gate = evaluate_gate(build_report(config, res, golden, n_boot=100))
    assert gate.passed and gate.exit_code == 0


def test_no_noise_floor_warns():
    config, res, golden = fixture()
    res = [r for r in res if not (r.candidate == "baseline" and r.replica == 1)]
    rep = build_report(config, res, golden, n_boot=50)
    assert rep.noise_floor is None and rep.noise_upper == 0.0
    assert any("no noise floor" in w for w in rep.warnings)


def test_renderings_deterministic_and_compact():
    config, res, golden = fixture()
    rep = build_report(config, res, golden, n_boot=200, target_precision=0.9)
    gate = evaluate_gate(rep)
    t1 = render_text(rep, gate)
    rep2 = build_report(config, list(reversed(res)), golden, n_boot=200, target_precision=0.9)
    assert render_text(rep2, evaluate_gate(rep2)) == t1
    md = render_markdown(rep, gate)
    assert md.count("\n") <= 60
    assert md.startswith("## Caliper eval: FAIL")
    assert "MISMATCH x2" in md and "MISMATCH x2" in t1
    assert "/Users" not in t1 + md and "run_id" not in t1


EXPECTED_MD = """\
## Caliper eval: FAIL

30 golden rows, reference `baseline`. noise floor (baseline x2, 30 rows): flip 3.3%, cost-\
weighted 0.050 [0.000, 0.150].

labels (30 rows): unknown 30 (source not recorded); NO HUMAN-TRUTH LABELS: label numbers b\
elow are agreement, not precision. Label a human-truth slice (label_source internal_review\
er / human_end_user).; splits: none; thresholds picked and reported on the same rows (opti\
mistic)

| candidate | rows | errors | cost $ | p95 ms | who answered | flips | regression [95% CI]\
 | excess vs noise (paired) | gate |
|---|---:|---:|---:|---:|---|---:|---|---|---|
| `baseline` (ref) | 30 | 0p/0d | 0.0120 | 148 | fake-large | - | - | - | PASS |
| `small` | 30 | 0p/0d | 0.0060 | 148 | fake-small | 10/30 | 0.667 [0.333, 1.000] | 0.617 \
[0.300, 0.901] | FAIL |
| `swapped` | 30 | 1p/1d | 0.0060 | 148 | fake-medium,fake-other (MISMATCH x2) | 0/29 | 0.\
000 [0.000, 0.000] | -0.052 [-0.155, 0.000] | FAIL |

**Changed outcomes vs reference**

- `small`: high -> low: 10
- `swapped`: none

**Calibration** (auto-accept when score >= threshold)

- `baseline`: 30 rows on all (agreement (unknown source)), ECE 0.167; picked 0.50: coverag\
e 66.7%, agreement (unknown source) 100.0%, approved sent to review 0.0%, kappa 1.000
- `small`: 30 rows on all (agreement (unknown source)), ECE 0.133; picked 0.50: coverage 3\
3.3%, agreement (unknown source) 100.0%, approved sent to review 50.0%, kappa 0.400
- `swapped`: 29 rows on all (agreement (unknown source)), ECE 0.169; picked 0.50: coverage\
 65.5%, agreement (unknown source) 100.0%, approved sent to review 0.0%, kappa 1.000

**Gate failures** (pass when paired excess upper bound <= 0.02)

- `small`: paired excess regression 0.617 [0.300, 0.901]: upper bound 0.901 > allowed 0.02\
0 (regression 0.667 - noise 0.050; 10 rows flipped)
- `swapped`: 2 replies served by a model other than requested (fake:fake-medium, fake:fake\
-other)
- `swapped`: error rate 3.3% > allowed 0.0% (no decision: 1, provider errors: 1)
"""


def test_markdown_golden():
    config, res, golden = fixture()
    rep = build_report(config, res, golden, n_boot=200, target_precision=0.9)
    assert render_markdown(rep, evaluate_gate(rep)) == EXPECTED_MD


EXPECTED_TEXT = """\
caliper eval: 3 candidates, 30 golden rows, reference=baseline, seed=0, bootstrap n=200
labels (30 rows): unknown 30 (source not recorded)
  NO HUMAN-TRUTH LABELS: label numbers below are agreement, not precision. Label a human-t\
ruth slice (label_source internal_reviewer / human_end_user).
splits: none; thresholds picked and reported on the same rows (optimistic)
noise floor (baseline x2, 30 rows): flip 3.3%, cost-weighted 0.050 [0.000, 0.150]

candidate       rows  errors  tokens in/out/cache  p50/p95 ms  cost $  who answered       \
                   flips  regression [95% CI]   excess vs noise (paired)  gate
--------------  ----  ------  -------------------  ----------  ------  -------------------\
-----------------  -----  --------------------  ------------------------  ----
baseline (ref)  30    0p/0d   6000/600/3000        125/148     0.0120  fake-large         \
                   -      -                     -                         PASS
small           30    0p/0d   3000/300/1500        125/148     0.0060  fake-small         \
                   10/30  0.667 [0.333, 1.000]  0.617 [0.300, 0.901]      FAIL
swapped         30    1p/1d   3000/300/1500        125/148     0.0060  fake-medium,fake-ot\
her (MISMATCH x2)  0/29   0.000 [0.000, 0.000]  -0.052 [-0.155, 0.000]    FAIL

transitions baseline -> small:
          high -> high            10
          high -> low             10  (cost 2 each)
           low -> low             10
  flipped: sku-001 (high->low), sku-004 (high->low), sku-007 (high->low), sku-010 (high->l\
ow), sku-013 (high->low), sku-016 (high->low), sku-019 (high->low), sku-022 (high->low) +2\
 more

transitions baseline -> swapped:
          high -> high            19
           low -> low             10
  errored rows: 1, missing rows: 0

calibration baseline (sweep on all: 30 rows labelled by unknown; ECE 0.167): picked 0.50 (\
lowest with agreement (unknown source) >= 90.0%)
  score >=  auto  coverage  agreement (unknown source)  approved->review  kappa
  --------  ----  --------  --------------------------  ----------------  -----
  0.50      20    66.7%     100.0%                      0.0%              1.000
  0.85      20    66.7%     100.0%                      0.0%              1.000

calibration small (sweep on all: 30 rows labelled by unknown; ECE 0.133): picked 0.50 (low\
est with agreement (unknown source) >= 90.0%)
  at 0.50: coverage 33.3%, agreement (unknown source) 100.0%, approved->review 50.0%, kapp\
a 0.400

calibration swapped (sweep on all: 29 rows labelled by unknown; ECE 0.169): picked 0.50 (l\
owest with agreement (unknown source) >= 90.0%)
  at 0.50: coverage 65.5%, agreement (unknown source) 100.0%, approved->review 0.0%, kappa\
 1.000

CI gate: FAIL (pass when paired excess upper bound <= 0.02)
  small: paired excess regression 0.617 [0.300, 0.901]: upper bound 0.901 > allowed 0.020 \
(regression 0.667 - noise 0.050; 10 rows flipped)
  swapped: 2 replies served by a model other than requested (fake:fake-medium, fake:fake-o\
ther)
  swapped: error rate 3.3% > allowed 0.0% (no decision: 1, provider errors: 1)
"""


def test_text_golden():
    config, res, golden = fixture()
    rep = build_report(config, res, golden, n_boot=200, target_precision=0.9)
    assert render_text(rep, evaluate_gate(rep)) == EXPECTED_TEXT


def test_candidate_sha_mismatch_is_stale():
    from caliper_eval.types import candidate_sha256

    config, res, golden = fixture()
    small = config.candidates[1]
    good = candidate_sha256(small)
    res = [
        r.model_copy(update={"candidate_sha256": good if r.row_id != "sku-000" else "old"})
        if r.candidate == "small"
        else r
        for r in res
    ]
    rep = build_report(config, res, golden, n_boot=20)
    assert rep.candidate("small").stale_results == 1
    assert rep.candidate("baseline").stale_results == 0  # None = not recorded, not stale
    gate = evaluate_gate(rep, max_regression=10.0)
    small_gate = next(g for g in gate.candidates if g.name == "small")
    assert any("candidate settings" in r for r in small_gate.reasons)
