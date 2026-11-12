"""Quality against human approvals: is a candidate *better* than the reference?

The regression measures elsewhere in Caliper treat the reference as right, so
they can only say "the same" or "worse". This module measures both sides
against people's verdicts on outcomes (:mod:`caliper_eval.approvals`). An
outcome a person rejected is an *error*; so is no decision at all.

**Improvement** (per candidate). On the ``N`` rows both sides were run on
(reference replica 0):

* **same** outcome: both are right or both are wrong; the row adds 0.
* **changed** outcome: the row needs a verdict on both outcomes. It is *fixed*
  (candidate approved, reference rejected), *broken* (the reverse), or a tie.

``improvement = (fixed - broken) / N``: how many fewer errors per row the
candidate makes. It is known only when every changed row has both verdicts (an
unreviewed change is unknown, not a tie). Its 95% interval is a bootstrap over
the ``N`` rows.

**The reference's error rate** (once per report). "X% fewer errors" needs it.
It is measured on the **base sample**: ``[quality] base_sample`` rows drawn
uniformly at random from the golden set with the config's seed, so ``caliper
review`` and ``caliper eval`` draw the same rows. Every sampled row needs a
verdict on the reference's outcome; rows reviewed for any other reason never
enter this estimate, because they were not chosen at random. Its 95% interval
is a Wilson score interval, which stays honest when the sample has no errors.

**Relative improvement** = improvement / reference error rate, with a
conservative interval: the low end divides the improvement's low end by the
error rate's high end (and so on). ``None`` for an unbounded end.

With ``[ci] quality_per_cost = k``, a candidate that costs ``ratio`` times the
reference, above ``1 + quality_cost_tolerance``, passes only when the relative
improvement's low end is at least ``k * (ratio - 1)`` and the improvement's low
end is above 0. Intervals are at ``[ci] quality_confidence`` (default 0.95; 0
compares point estimates).
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from statistics import NormalDist

from pydantic import BaseModel, ConfigDict, Field

from ._stats import CI, DEFAULT_CONFIDENCE, DEFAULT_N_BOOT, derive_seed, quantile
from .approvals import Verdicts
from .noise import index_results
from .types import GoldenRow, RowResult

MAX_LISTED_ROWS = 20
Z_95 = 1.959963984540054


class Bounds(BaseModel):
    """An estimate with an interval whose ends may be unbounded (``None``)."""

    model_config = ConfigDict(extra="forbid")

    estimate: float | None  # point estimate; None when undefined (e.g. divide by 0)
    low: float | None  # lower end; None = unbounded below
    high: float | None  # upper end; None = unbounded above


class BaseRate(BaseModel):
    """The reference's error rate, on a uniform random sample of golden rows."""

    model_config = ConfigDict(extra="forbid")

    n_sample: int  # rows drawn ([quality] base_sample, at most the golden set)
    n_not_run: int  # sampled rows with no reference result (excluded)
    n_judged: int  # sampled rows with a verdict on the reference's outcome (or no decision)
    n_errors: int  # judged rows where the reference's outcome was rejected or missing
    unreviewed_rows: list[str] = Field(default_factory=list)  # sampled rows still to review
    error_rate: CI | None = None  # Wilson interval; None until every sampled row is judged


class QualityComparison(BaseModel):
    """A candidate against the reference, both judged by human approvals."""

    model_config = ConfigDict(extra="forbid")

    n_rows: int  # rows run on both sides (reference replica 0)
    n_excluded: int  # rows missing a result on one side (not run)
    n_changed: int  # rows where the outcomes differ (no decision counts as an outcome)
    n_changed_reviewed: int  # changed rows with a verdict on both sides
    fixed: int  # changed rows: candidate right, reference wrong
    broken: int  # changed rows: reference right, candidate wrong
    both_approved: int  # changed rows where both outcomes are acceptable
    both_rejected: int  # changed rows where neither outcome is acceptable
    unreviewed_rows: list[str] = Field(default_factory=list)  # changed rows missing a verdict
    improvement: CI | None = None  # (fixed - broken) / n_rows: fewer errors per row
    relative_improvement: Bounds | None = None  # improvement / the reference's error rate
    required_relative: float | None = None  # quality_per_cost x (cost ratio - 1)


def base_sample_rows(golden: Sequence[GoldenRow], size: int, seed: int) -> list[str]:
    """The base sample: ``size`` golden row ids drawn uniformly with ``seed``."""
    ids = sorted(g.id for g in golden)
    rng = random.Random(derive_seed(seed, "base-sample"))  # noqa: S311 - sampling, not crypto
    return sorted(rng.sample(ids, min(max(size, 0), len(ids))))


def z_for(confidence: float) -> float:
    """Two-sided normal quantile for ``confidence`` (0 gives 0: the point estimate)."""
    return NormalDist().inv_cdf(0.5 + confidence / 2) if confidence > 0 else 0.0


def wilson(errors: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a proportion ``errors / n`` (``n > 0``)."""
    p = errors / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def compute_base_rate(
    golden: Sequence[GoldenRow],
    results: Iterable[RowResult],
    verdicts: Verdicts,
    reference: str,
    *,
    size: int,
    seed: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> BaseRate:
    """The reference's error rate on the base sample (see the module docstring)."""
    idx = index_results(results)
    rows = base_sample_rows(golden, size, seed)
    not_run = judged = errors = 0
    unreviewed: list[str] = []
    for row_id in rows:
        r = idx.get((reference, row_id, 0))
        if r is None:
            not_run += 1
            continue
        if r.decision is None:
            judged += 1
            errors += 1
            continue
        v = verdicts.get(row_id, r.decision.outcome)
        if v is None:
            unreviewed.append(row_id)
            continue
        judged += 1
        errors += int(not v)
    out = BaseRate(
        n_sample=len(rows),
        n_not_run=not_run,
        n_judged=judged,
        n_errors=errors,
        unreviewed_rows=unreviewed[:MAX_LISTED_ROWS],
    )
    if judged and not unreviewed:
        low, high = wilson(errors, judged, z_for(confidence))
        out.error_rate = CI(
            estimate=errors / judged, low=low, high=high, confidence=confidence, n_boot=0
        )
    return out


def _ci(stats: list[float], estimate: float, confidence: float) -> CI:
    if confidence <= 0:  # a zero-width interval is the point estimate itself
        return CI(estimate=estimate, low=estimate, high=estimate, confidence=0, n_boot=len(stats))
    stats.sort()
    alpha = (1 - confidence) / 2
    return CI(
        estimate=estimate,
        low=quantile(stats, alpha),
        high=quantile(stats, 1 - alpha),
        confidence=confidence,
        n_boot=len(stats),
    )


def _divide(imp: CI, base: CI) -> Bounds:
    """Conservative interval of ``imp / base`` for ``base`` in [0, 1]."""

    def div(x: float, y: float) -> float | None:
        return None if y <= 0 else x / y

    estimate = div(imp.estimate, base.estimate)
    low = div(imp.low, base.high) if imp.low >= 0 else div(imp.low, base.low)
    high = div(imp.high, base.low) if imp.high >= 0 else div(imp.high, base.high)
    if imp.low >= 0 and low is None:
        low = 0.0  # a zero error rate everywhere: no relative gain can be shown
    if imp.high < 0 and high is None:
        high = 0.0
    return Bounds(estimate=estimate, low=low, high=high)


def compute_quality(
    results: Iterable[RowResult],
    verdicts: Verdicts,
    candidate: str,
    reference: str,
    *,
    base: BaseRate | None = None,
    required_relative: float | None = None,
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
    confidence: float = DEFAULT_CONFIDENCE,
) -> QualityComparison:
    """Compare ``candidate`` with ``reference`` on human approvals of their outcomes."""
    idx = index_results(results)
    diffs: list[int] = []  # per row: error(ref) - error(cand), 0 for same outcomes
    excluded = 0
    unreviewed: list[str] = []
    n_changed = fixed = broken = both_ok = both_bad = 0
    for (cand, row_id, replica), res in sorted(idx.items()):
        if cand != candidate or replica != 0:
            continue
        ref = idx.get((reference, row_id, 0))
        if ref is None:
            excluded += 1
            continue
        a = None if ref.decision is None else ref.decision.outcome
        b = None if res.decision is None else res.decision.outcome
        if a == b:
            diffs.append(0)
            continue
        n_changed += 1
        # No decision is an error: nothing reached the user, or a fallback did.
        ea = 1 if a is None else _error(verdicts.get(row_id, a))
        eb = 1 if b is None else _error(verdicts.get(row_id, b))
        if ea is None or eb is None:
            unreviewed.append(row_id)
            continue
        diffs.append(ea - eb)
        if ea and not eb:
            fixed += 1
        elif eb and not ea:
            broken += 1
        elif ea:
            both_bad += 1
        else:
            both_ok += 1
    for cand, row_id, replica in idx:
        if cand == reference and replica == 0 and (candidate, row_id, 0) not in idx:
            excluded += 1

    n = len(diffs) + len(unreviewed)
    out = QualityComparison(
        n_rows=n,
        n_excluded=excluded,
        n_changed=n_changed,
        n_changed_reviewed=n_changed - len(unreviewed),
        fixed=fixed,
        broken=broken,
        both_approved=both_ok,
        both_rejected=both_bad,
        unreviewed_rows=unreviewed[:MAX_LISTED_ROWS],
        required_relative=required_relative,
    )
    if n == 0 or unreviewed:
        return out
    estimate = sum(diffs) / n
    draws = []
    rng = random.Random(derive_seed(seed, "quality", candidate))  # noqa: S311 - stats
    for _ in range(max(n_boot, 0)):
        draws.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    out.improvement = (
        _ci(draws, estimate, confidence)
        if draws
        else CI(estimate=estimate, low=estimate, high=estimate, confidence=confidence, n_boot=0)
    )
    if base is not None and base.error_rate is not None:
        out.relative_improvement = _divide(out.improvement, base.error_rate)
    return out


def _error(verdict: bool | None) -> int | None:
    return None if verdict is None else int(not verdict)


def quality_passes(q: QualityComparison) -> bool:
    """The ``quality_per_cost`` rule: shown better by at least the required share."""
    if q.improvement is None or q.relative_improvement is None or q.required_relative is None:
        return False
    low = q.relative_improvement.low
    # "10% more cost for 10% fewer errors" is a pass: compare with float slack.
    return q.improvement.low > 0 and low is not None and low >= q.required_relative - 1e-9


__all__ = [
    "BaseRate",
    "Bounds",
    "QualityComparison",
    "base_sample_rows",
    "compute_base_rate",
    "compute_quality",
    "quality_passes",
    "wilson",
    "z_for",
]
