"""The catalog-cleanup feature's system logic, mirrored for Caliper.

The production feature asks the model three times per supplier listing and
then decides what to do with the record:

1. A reply counts only if it parses as a JSON object with a non-empty
   ``description``, a ``tax_code`` from the (fictional) CTX code list, and a
   numeric ``confidence`` in [0, 1].
2. Fewer than two usable replies -> ``fallback`` (the record goes to the
   manual-entry queue with nothing pre-filled).
3. If at least two usable replies agree on the tax code and that code has a
   strict plurality, the score is the mean confidence of the agreeing
   replies. Otherwise the replies disagree -> ``needs_review`` with score 0.
4. Score >= ``THRESHOLD`` -> ``auto_publish``; below -> ``needs_review``.

The tax code itself is reported in ``Decision.detail``, not in the outcome:
transitions are computed on the routing decision (publish / review /
fallback). A run where a candidate auto-publishes a *different* code than
the reference shows up in ``detail``, not in the transition matrix.

This file mirrors ``feature_rule.py`` (the production-shaped rule). The
parity test in ``tests/test_parity.py`` proves they agree on every reply
sequence built from a representative alphabet.

Self-contained on purpose: Caliper loads it by path.
"""

from __future__ import annotations

import json
import math
from typing import Any

from caliper_eval.types import Decision, Reply

THRESHOLD = 0.75
MIN_AGREE = 2
MIN_VALID = 2

# Fictional tax-category scheme used only in this example.
TAX_CODES = frozenset(
    {
        "CTX-100",  # general merchandise
        "CTX-210",  # packaged food
        "CTX-310",  # printed books and maps
        "CTX-420",  # children's clothing
        "CTX-430",  # adult clothing and footwear
        "CTX-510",  # small electronics
        "CTX-520",  # loose batteries and power cells
        "CTX-610",  # home health devices
        "CTX-900",  # gift cards and vouchers
    }
)


def _payload(reply: Reply) -> Any:
    if reply.error:
        return None
    if reply.parsed is not None:
        return reply.parsed
    if reply.text is None:
        return None
    try:
        return json.loads(reply.text)
    except ValueError:
        return None


def _usable(obj: Any) -> tuple[str, float] | None:
    if not isinstance(obj, dict):
        return None
    code = obj.get("tax_code")
    conf = obj.get("confidence")
    desc = obj.get("description")
    if not isinstance(code, str) or code not in TAX_CODES:
        return None
    if isinstance(conf, bool) or not isinstance(conf, int | float):
        return None
    if not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        return None
    if not isinstance(desc, str) or not desc.strip():
        return None
    return code, float(conf)


def decide(replies: list[Reply]) -> Decision:
    votes = [v for v in (_usable(_payload(r)) for r in replies) if v is not None]
    if len(votes) < MIN_VALID:
        return Decision(
            outcome="fallback",
            score=None,
            detail={"reason": "too_few_usable_replies", "usable": len(votes)},
        )

    counts: dict[str, int] = {}
    for code, _ in votes:
        counts[code] = counts.get(code, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top_code, top_n = ranked[0]
    tied = len(ranked) > 1 and ranked[1][1] == top_n
    if top_n < MIN_AGREE or tied:
        return Decision(
            outcome="needs_review",
            score=0.0,
            detail={"reason": "replies_disagree", "usable": len(votes), "votes": counts},
        )

    agreeing = [conf for code, conf in votes if code == top_code]
    score = sum(agreeing) / len(agreeing)
    outcome = "auto_publish" if score >= THRESHOLD else "needs_review"
    return Decision(
        outcome=outcome,
        score=round(score, 4),
        detail={
            "tax_code": top_code,
            "agree": top_n,
            "usable": len(votes),
            "reason": "confident" if outcome == "auto_publish" else "low_confidence",
        },
    )
