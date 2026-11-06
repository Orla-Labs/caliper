"""decide.py mirrors feature_rule.py exactly, over every reply sequence.

Caliper grades what ``decide`` returns, so ``decide`` must do what the
production rule does. ``assert_parity`` enumerates every sequence of 0-3
replies built from the alphabet below (the production feature asks three
times) and compares both sides. The compared value folds in the tax code and
the score, not only the routing outcome, so a mirror that routes correctly but
reports a different code or score still fails.

The alphabet holds one reply of each shape that matters to the rule,
including the edges that tend to split two implementations: confidence
exactly at the threshold, a bool confidence (``true`` is an int in Python),
NaN, out-of-range values, unknown codes, truncated JSON, transport errors and
structured (parsed-only) replies.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from caliper_eval.decision import ParityError, assert_parity
from caliper_eval.types import Reply

HERE = Path(__file__).resolve().parent.parent


def _load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


decide_mod = _load("decide", "catalog_decide_under_test")
rule_mod = _load("feature_rule", "catalog_feature_rule_under_test")

ACTION_TO_OUTCOME = {"publish": "auto_publish", "review": "needs_review", "manual": "fallback"}


def _rec(code="CTX-510", conf=0.9, desc="Fennick smart plug") -> str:
    return json.dumps({"description": desc, "specs": {}, "tax_code": code, "confidence": conf})


ALPHABET = [
    Reply(text=_rec(conf=0.9)),  # confident, code A
    Reply(text=_rec(conf=0.6)),  # unsure, code A
    Reply(text=_rec(conf=0.75)),  # exactly at the threshold
    Reply(text=_rec(code="CTX-520", conf=0.95)),  # confident, code B
    Reply(parsed=json.loads(_rec(conf=0.8))),  # structured output: parsed only, no text
    Reply(text=_rec(conf=0.9)[:30]),  # truncated JSON
    Reply(error="overloaded"),  # transport/provider error
    Reply(text=_rec(conf=True)),  # bool confidence
    Reply(text='{"description": "x", "tax_code": "CTX-510", "confidence": NaN}'),
    Reply(text=_rec(conf=1.5)),  # out of range
    Reply(text=_rec(code="CTX-999")),  # not in the code list
    Reply(text=_rec(desc="  ")),  # blank description
]


def mirror(replies: list[Reply]) -> str:
    d = decide_mod.decide(replies)
    return f"{d.outcome}|{d.detail.get('tax_code')}|{d.score}"


def raw_strings(replies: list[Reply]) -> list[str | None]:
    """What the production HTTP client hands the rule: body text, or None on failure."""
    out = []
    for r in replies:
        if r.error:
            out.append(None)
        elif r.text is not None:
            out.append(r.text)
        elif r.parsed is not None:
            out.append(json.dumps(r.parsed))
        else:
            out.append(None)
    return out


def real(raw: list[str | None]) -> str:
    res = rule_mod.route_listing(raw)
    return f"{ACTION_TO_OUTCOME[res['action']]}|{res['tax_code']}|{res['score']}"


def test_decide_mirrors_the_production_rule():
    checked = assert_parity(mirror, real, ALPHABET, length=[0, 1, 2, 3], adapter=raw_strings)
    assert checked == 1 + 12 + 12**2 + 12**3


def test_parity_catches_an_off_by_one_threshold():
    """A mirror that publishes only *above* 0.75 (not at it) is caught."""
    broken = _load("decide", "catalog_decide_broken")
    broken.THRESHOLD = 0.7500001

    def broken_mirror(replies: list[Reply]) -> str:
        d = broken.decide(replies)
        return f"{d.outcome}|{d.detail.get('tax_code')}|{d.score}"

    with pytest.raises(ParityError) as err:
        assert_parity(broken_mirror, real, ALPHABET, length=3, adapter=raw_strings)
    assert err.value.mirror_outcome.startswith("needs_review")
    assert err.value.real_outcome.startswith("auto_publish")
