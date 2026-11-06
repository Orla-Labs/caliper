import pytest
from caliper_eval.decision import ParityError, assert_parity, coerce_decision, decision
from caliper_eval.types import Decision, Reply


def test_coerce_variants():
    assert coerce_decision("yes").outcome == "yes"
    d = coerce_decision(("high", 0.9))
    assert d.outcome == "high" and d.score == 0.9
    assert coerce_decision({"outcome": "x", "score": 0.1}).score == 0.1
    same = Decision(outcome="z")
    assert coerce_decision(same) is same
    with pytest.raises(TypeError):
        coerce_decision(3)


def test_decorator_returns_decision():
    @decision
    def decide(replies):
        return "pos" if any(r.text == "yes" for r in replies) else "neg"

    out = decide([Reply(text="no"), Reply(text="yes")])
    assert isinstance(out, Decision) and out.outcome == "pos"
    assert decide.__name__ == "decide"


@decision
def mirror_vote(replies):
    """Majority of 3 with early stop after 2 agreeing; error counts as 'unsure'."""
    votes = [r.text if r.error is None else "unsure" for r in replies]
    yes = votes.count("yes")
    no = votes.count("no")
    if yes >= 2:
        return "positive"
    if no >= 2:
        return "negative"
    return "unsure"


def real_vote(texts):
    votes = ["unsure" if t is None else t for t in texts]
    if votes.count("yes") >= 2:
        return "positive"
    if votes.count("no") >= 2:
        return "negative"
    return "unsure"


ALPHABET = ["yes", "no", "maybe", Reply(error="timeout")]


def test_parity_passes_with_adapter():
    n = assert_parity(
        mirror_vote,
        real_vote,
        ALPHABET,
        length=[2, 3],
        adapter=lambda rs: [r.text for r in rs],
    )
    assert n == 4**2 + 4**3


def test_parity_reports_first_counterexample():
    def buggy_real(texts):
        # treats "maybe" as a yes vote
        votes = ["yes" if t == "maybe" else t for t in texts]
        return real_vote(votes)

    with pytest.raises(ParityError) as ei:
        assert_parity(
            mirror_vote, buggy_real, ALPHABET, length=3, adapter=lambda rs: [r.text for r in rs]
        )
    err = ei.value
    assert err.mirror_outcome != err.real_outcome
    assert [r.text for r in err.replies] == ["yes", "no", "maybe"]


def test_parity_exception_on_one_side_is_mismatch():
    def raising(_):
        raise RuntimeError("boom")

    with pytest.raises(ParityError, match="real raised RuntimeError"):
        assert_parity(mirror_vote, raising, ["yes"], length=1)


def test_parity_blowup_guard():
    with pytest.raises(ValueError, match="max_sequences"):
        assert_parity(mirror_vote, real_vote, ["a", "b", "c", "d"], length=12)


def test_parity_rejects_empty_alphabet():
    with pytest.raises(ValueError):
        assert_parity(mirror_vote, real_vote, [], length=1)
