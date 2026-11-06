from caliper_eval._stats import bootstrap_mean_ci, derive_seed, nearest_rank, transition_cost
from caliper_eval.noise import compute_noise_floor
from caliper_eval.transitions import compute_transitions
from caliper_eval.types import Decision, Reply, RowResult


def rr(cand, row, outcome, replica=0, score=None):
    return RowResult(
        run_id="t",
        candidate=cand,
        row_id=row,
        replica=replica,
        request_sha256="x",
        replies=[Reply(text=outcome or "")],
        decision=None if outcome is None else Decision(outcome=outcome, score=score),
        error=None if outcome is not None else "decide failed",
    )


COSTS = {"positive->negative": 5.0, "negative->positive": 1.0}


def test_transition_cost_defaults():
    assert transition_cost(COSTS, "a", "a") == 0.0
    assert transition_cost(COSTS, "positive", "negative") == 5.0
    assert transition_cost(COSTS, "x", "y") == 1.0


def test_bootstrap_is_deterministic_and_brackets_mean():
    vals = [0.0] * 90 + [1.0] * 10
    a = bootstrap_mean_ci(vals, seed=derive_seed(0, "x"), n_boot=500)
    b = bootstrap_mean_ci(vals, seed=derive_seed(0, "x"), n_boot=500)
    assert a == b
    assert a.low <= a.estimate == 0.1 <= a.high
    assert 0.03 < a.low < 0.1 < a.high < 0.17
    empty = bootstrap_mean_ci([], seed=1)
    assert empty.estimate == empty.low == empty.high == 0.0


def test_nearest_rank():
    assert nearest_rank([5, 1, 3, 2, 4], 50) == 3
    assert nearest_rank([5, 1, 3, 2, 4], 95) == 5
    assert nearest_rank([], 50) == 0.0


def _reference_results():
    out = []
    for i in range(20):
        row = f"r{i:02d}"
        o0 = "positive" if i < 5 else "negative"
        o1 = o0
        if i == 0:
            o1 = "negative"  # one replay flip, asymmetric cost pair
        out += [rr("ref", row, o0, 0), rr("ref", row, o1, 1)]
    out.append(rr("ref", "r20", "negative", 0))  # incomplete row
    out += [rr("ref", "r21", "negative", 0), rr("ref", "r21", None, 1)]  # errored
    return out


def test_noise_floor_two_replicas():
    nf = compute_noise_floor(_reference_results(), "ref", costs=COSTS, seed=0, n_boot=200)
    assert nf is not None
    assert nf.n_replicas == 2
    assert nf.n_rows == 20
    assert nf.n_incomplete_rows == 1
    assert nf.errored_rows == ["r21"]
    assert nf.flipped_rows == ["r00"]
    assert nf.flip_rate.estimate == 1 / 20
    # symmetrized: (5 + 1) / 2 = 3 for the one flipped row
    assert nf.cost_weighted_flip_rate.estimate == 3 / 20


def test_noise_floor_symmetric_in_replica_order():
    swapped = []
    for r in _reference_results():
        swapped.append(r.model_copy(update={"replica": 1 - r.replica if r.row_id != "r20" else 0}))
    a = compute_noise_floor(_reference_results(), "ref", costs=COSTS, n_boot=0)
    b = compute_noise_floor(swapped, "ref", costs=COSTS, n_boot=0)
    assert a.cost_weighted_flip_rate.estimate == b.cost_weighted_flip_rate.estimate


def test_noise_floor_n_replicas_pairwise_vs_zero():
    res = [
        rr("ref", "a", "x", 0),
        rr("ref", "a", "x", 1),
        rr("ref", "a", "y", 2),
        rr("ref", "b", "x", 0),
        rr("ref", "b", "x", 1),
        rr("ref", "b", "x", 2),
    ]
    nf = compute_noise_floor(res, "ref", n_boot=0)
    assert nf.n_replicas == 3
    assert nf.flip_rate.estimate == (0.5 + 0.0) / 2


def test_noise_floor_none_without_replays():
    assert compute_noise_floor([rr("ref", "a", "x")], "ref") is None


def test_transitions_matrix_errors_and_missing():
    res = _reference_results()
    for i in range(20):
        row = f"r{i:02d}"
        o = "positive" if i < 5 else "negative"
        if i in (1, 2):
            o = "negative"  # dropped positives (cost 5 each)
        if i == 10:
            o = "positive"  # added positive (cost 1)
        if i == 11:
            o = "unsure"  # unlisted change (cost 1)
        res.append(rr("cand", row, o))
    res.append(rr("cand", "r21", "negative"))  # ref replica 0 ok, decided both sides
    res.append(rr("cand", "r20", None))  # cand errored
    res.append(rr("cand", "extra", "negative"))  # missing on ref side
    t = compute_transitions(res, "cand", "ref", costs=COSTS, seed=0, n_boot=200)
    assert t.n_rows == 21
    assert t.n_errored == 1 and t.errored_rows == ["r20"]
    assert t.n_missing == 1
    by = {(m.src, m.dst): m.count for m in t.matrix}
    assert by[("positive", "negative")] == 2
    assert by[("negative", "positive")] == 1
    assert by[("negative", "unsure")] == 1
    assert by[("positive", "positive")] == 3
    assert [f.row_id for f in t.flipped] == ["r01", "r02", "r10", "r11"]
    assert t.regression.estimate == (5 + 5 + 1 + 1) / 21
    assert t.flip_rate.estimate == 4 / 21
    assert t.regression.low <= t.regression.estimate <= t.regression.high


def test_candidate_ci_independent_of_other_candidates():
    base = _reference_results()
    cand = [rr("cand", f"r{i:02d}", "negative") for i in range(20)]
    other = [rr("other", f"r{i:02d}", "positive") for i in range(20)]
    a = compute_transitions(base + cand, "cand", "ref", seed=3)
    b = compute_transitions(base + other + cand, "cand", "ref", seed=3)
    assert a == b
