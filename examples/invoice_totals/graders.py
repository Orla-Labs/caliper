"""Graders for the invoice_totals example.

Task: read a short synthetic invoice and end the answer with exactly one
``<total>AMOUNT</total>`` tag holding the total amount due (e.g.
``<total>123.45</total>``).

Three graders, from worst to best:

* ``length_baseline`` -- a leakage baseline. It never looks at ``gold``; it
  scores surface features only (has a ``<total>`` tag, is short). Whatever
  agreement it reaches with human labels is the floor any real grader must
  clear by a wide margin.
* ``naive_grader`` -- the grader most people write first: "is the gold answer
  somewhere in the response?". Plausible, and reward-hackable: listing several
  candidate numbers, or answering with a bare number, still scores 1.0.
* ``fixed_grader`` -- strict format gate plus exact parse of a single answer.
  Exactly one ``<total>`` tag, containing exactly one number, numerically equal
  to gold. Prose around the tag is ignored, tag case is ignored.

Graders read only ``rollout.response`` and ``rollout.gold``. They never read
``rollout.metadata`` (the human label lives there in the labeled set).

This file imports only ``caliper_sdk`` and the standard library so it can be
inlined into a self-contained reward module by ``caliper-compile``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from caliper_sdk import GraderResult, Rollout, grader

_OPEN_TAG_RE = re.compile(r"<total>", re.IGNORECASE)
_TAG_RE = re.compile(r"<total>(.*?)</total>", re.IGNORECASE | re.DOTALL)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# length_baseline: responses with at most this many whitespace-separated
# tokens count as "short". Token count (not characters) keeps the baseline
# invariant to whitespace and case perturbations.
_MAX_WORDS = 40


@grader(name="naive_grader", version="1.0.0")
def naive_grader(rollout: Rollout) -> GraderResult:
    """1.0 if the gold amount appears anywhere in the response, else 0.0."""
    if rollout.gold and rollout.gold in rollout.response:
        return GraderResult(score=1.0, explanation="gold found in response")
    return GraderResult(score=0.0, explanation="gold not found in response")


@grader(name="fixed_grader", version="1.0.0")
def fixed_grader(rollout: Rollout) -> GraderResult:
    """1.0 iff there is exactly one <total> tag holding one number equal to gold."""
    if not rollout.gold:
        return GraderResult(score=0.0, explanation="no gold answer on this row")

    n_open = len(_OPEN_TAG_RE.findall(rollout.response))
    tags = _TAG_RE.findall(rollout.response)
    if n_open != 1 or len(tags) != 1:
        return GraderResult(
            score=0.0,
            explanation=f"format gate: expected exactly one <total> tag, found {n_open}",
        )

    inner = tags[0].strip()
    if not _NUMBER_RE.fullmatch(inner):
        return GraderResult(
            score=0.0, explanation=f"format gate: tag must hold one number, got {inner!r}"
        )

    try:
        answer = Decimal(inner)
        gold = Decimal(rollout.gold)
    except InvalidOperation:
        return GraderResult(score=0.0, explanation="could not parse amount")

    if answer == gold:
        return GraderResult(score=1.0, explanation="tagged amount equals gold")
    return GraderResult(score=0.0, explanation=f"tagged amount {inner} != gold")


@grader(name="length_baseline", version="1.0.0")
def length_baseline(rollout: Rollout) -> GraderResult:
    """Surface-feature baseline: 1.0 for short responses with a <total> tag.

    Ignores gold entirely. If this scores well against human labels, the
    labels are predictable from surface features and tell you little.
    """
    has_tag = "<total>" in rollout.response.lower()
    short = len(rollout.response.split()) <= _MAX_WORDS
    if has_tag and short:
        return GraderResult(score=1.0, explanation="short and tagged")
    return GraderResult(score=0.0, explanation="untagged or long")
