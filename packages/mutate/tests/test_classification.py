"""Classification-grader mutations: unit behaviour + the naive/robust smoke example."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from caliper_mutate import (
    CLASSIFICATION_MUTATION_TYPES,
    ClassPriorAnswer,
    ConstantAnswer,
    Hedging,
    Mutation,
    MutationExpectation,
    SurfaceFeatureOnly,
    TaggedVerdictFormat,
    VerdictRationaleContradiction,
    classification_mutations,
    majority_class,
)
from caliper_sdk import EvalRow, Rollout

LABELS = ["spam", "ham"]


def _ro(verdict: str = "ham", rationale: str = "Says \"see you at lunch\", so ham.", **extra):
    body = {"verdict": verdict, "rationale": rationale, **extra}
    return Rollout(prompt="Is this spam? see you at lunch", response=json.dumps(body), gold="ham")


def _out(rollout: Rollout) -> dict:
    return json.loads(rollout.response)


def test_all_lower_and_satisfy_protocol() -> None:
    muts = classification_mutations(LABELS, keywords={"win": "spam"}, majority_label="ham")
    assert [m.name for m in muts] == [
        "constant_answer",
        "class_prior_answer",
        "surface_feature_only",
        "verdict_rationale_contradiction",
        "hedging",
    ]
    for m in muts:
        assert isinstance(m, Mutation)
        assert m.expected_effect is MutationExpectation.LOWER
        assert m.description
    assert {type(m) for m in muts} == set(CLASSIFICATION_MUTATION_TYPES)


def test_keywords_optional_and_majority_required() -> None:
    muts = classification_mutations(LABELS, majority_label="ham")
    assert "surface_feature_only" not in [m.name for m in muts]
    with pytest.raises(ValueError, match="majority_label or eval_set"):
        classification_mutations(LABELS)
    with pytest.raises(ValueError):
        classification_mutations(["only"], majority_label="only")


def test_constant_answer_drops_item_specifics_and_keeps_extra_keys() -> None:
    ro = _ro(confidence=0.9)
    out = _out(ConstantAnswer("spam").apply(ro))
    assert out == {"verdict": "spam", "rationale": "Classified as spam.", "confidence": 0.9}
    assert _out(ro)["verdict"] == "ham"  # input not mutated


def test_class_prior_from_eval_set() -> None:
    rows = [
        EvalRow(id=str(i), rollout=Rollout(prompt="p", response="{}", gold=g))
        for i, g in enumerate(["ham", "spam", "spam", "ham"])
    ]
    assert majority_class(rows) == "ham"  # tie -> alphabetical
    m = ClassPriorAnswer.from_eval_set(rows)
    assert _out(m.apply(_ro()))["verdict"] == "ham"
    assert "Most items are ham" in _out(m.apply(_ro()))["rationale"]
    with pytest.raises(ValueError):
        majority_class([])


def test_surface_feature_first_keyword_then_fallback() -> None:
    m = SurfaceFeatureOnly({"LUNCH": "ham", "see": "spam"}, "spam")
    assert _out(m.apply(_ro())) == {"verdict": "ham", "rationale": "The item mentions 'LUNCH'."}
    m2 = SurfaceFeatureOnly({"prize": "spam"}, "ham")
    assert _out(m2.apply(_ro()))["rationale"] == "No keyword matched."
    with pytest.raises(ValueError):
        SurfaceFeatureOnly({}, "ham")


def test_contradiction_keeps_verdict_and_argues_next_label() -> None:
    out = _out(VerdictRationaleContradiction(LABELS).apply(_ro()))
    assert out["verdict"] == "ham"
    assert out["rationale"].endswith("So this is not ham; it is spam.")
    out2 = _out(VerdictRationaleContradiction(LABELS).apply(_ro(verdict="spam")))
    assert out2["rationale"].endswith("it is ham.")  # wraps around
    out3 = _out(VerdictRationaleContradiction(LABELS).apply(_ro(verdict="maybe")))
    assert out3["rationale"].endswith("it is spam.")


def test_hedging_names_every_label() -> None:
    out = _out(Hedging(LABELS).apply(_ro()))
    assert out["verdict"] == "spam or ham"
    assert out["rationale"].startswith("Hard to say; it could be spam or ham.")


@pytest.mark.parametrize("response", ["not json", "[1, 2]", '{"rationale": "x"}', ""])
def test_unparseable_is_a_noop(response: str) -> None:
    ro = Rollout(prompt="p", response=response)
    for m in classification_mutations(LABELS, keywords={"p": "spam"}, majority_label="ham"):
        assert m.apply(ro).response == response


def test_tagged_format_roundtrip() -> None:
    fmt = TaggedVerdictFormat()
    ro = Rollout(prompt="p", response="Verdict: ham\nRationale: friendly\nnote, two lines")
    assert fmt.parse(ro.response) == ("ham", "friendly\nnote, two lines")
    out = Hedging(LABELS, fmt=fmt).apply(ro).response
    assert out.startswith("Verdict: spam or ham\nRationale: Hard to say")
    assert fmt.parse("no verdict here") is None


def test_deterministic() -> None:
    muts = classification_mutations(LABELS, keywords={"lunch": "ham"}, majority_label="ham")
    assert [m.apply(_ro()).response for m in muts] == [m.apply(_ro()).response for m in muts]


def _load_smoke() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "smoke_mutate_classification.py"
    spec = importlib.util.spec_from_file_location("smoke_mutate_classification", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_naive_survives_robust_kills() -> None:
    smoke = _load_smoke()
    reports = smoke.run()
    naive = reports["naive_classifier_grader"]
    robust = reports["robust_classifier_grader"]
    assert smoke.survivors(naive) == [m.name for m in naive.per_mutation]
    assert len(naive.per_mutation) == 5
    assert smoke.survivors(robust) == []
    assert all(s.n_applied == len(smoke.TICKETS) for s in robust.per_mutation)
    assert not robust.failures
