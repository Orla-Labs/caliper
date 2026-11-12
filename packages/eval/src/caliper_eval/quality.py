"""Quality against human approvals: is a candidate *better* than the reference?

The regression measures elsewhere in Caliper treat the reference as right, so
they can only say "the same" or "worse". This module measures both sides
against people's verdicts on outcomes (:mod:`caliper_eval.approvals`).

On every row decided by both the reference (replica 0) and the candidate:

* **same** outcome: both are right or both are wrong. The row adds nothing to
  the difference, whether or not anyone reviewed it.
* **changed** outcome: the row needs a verdict on both outcomes. It is
  *fixed* (candidate approved, reference rejected), *broken* (the reverse),
  or a tie (both approved, both rejected).

Estimates, over the ``N`` compared rows (``D`` changed, ``S`` same)::

    error(side)  = 1 when a person rejected that side's outcome on the row
    improvement  = sum over D of (error(ref) - error(cand)) / N
    ref errors   = (sum over D of error(ref) + |S| * mean over reviewed S of error(ref)) / N
    relative     = improvement / ref errors

``improvement`` needs every changed row reviewed: an unreviewed change is not
a tie, so it stays unknown and fails the gate. The reference's error rate needs
reviewed rows in ``S`` as well (a random sample, e.g. ``caliper review
--base-sample``); they are treated as a random sample of ``S``.

Intervals come from one bootstrap that resamples the ``N`` rows: every draw
recomputes the improvement, the reference's error rate, their ratio and the
gate margin on the same rows. Draws whose reference error rate is undefined
(no reviewed ``S`` row drawn) or zero (for the ratio) are skipped.

With ``[ci] quality_per_cost = k``, a candidate that costs ``ratio`` times the
reference must be at least ``k * (ratio - 1)`` better, relative to the
reference's error rate. The gate reads ``margin = improvement - k * (ratio - 1)
* ref errors``: it passes when the margin's lower bound is at least 0 and the
improvement's lower bound is above 0.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from ._stats import CI, DEFAULT_CONFIDENCE, DEFAULT_N_BOOT, derive_seed, quantile
from .approvals import Verdicts
from .noise import index_results
from .types import RowResult

# Below this share of defined bootstrap draws, an estimate is reported as unknown.
MIN_DEFINED_DRAWS = 0.5
MAX_LISTED_ROWS = 20


class QualityComparison(BaseModel):
    """A candidate against the reference, both judged by human approvals."""

    model_config = ConfigDict(extra="forbid")

    n_rows: int  # rows decided by both sides (reference replica 0)
    n_excluded: int  # rows with no decision on one side
    n_changed: int  # rows where the outcomes differ
    n_changed_reviewed: int  # changed rows with a verdict on both outcomes
    fixed: int  # changed rows: candidate approved, reference rejected
    broken: int  # changed rows: reference approved, candidate rejected
    both_approved: int  # changed rows where both outcomes are acceptable
    both_rejected: int  # changed rows where neither outcome is acceptable
    unreviewed_rows: list[str] = Field(default_factory=list)  # changed rows missing a verdict
    n_same: int  # rows where both decide the same outcome
    n_same_reviewed: int  # same rows with a verdict on that outcome
    same_rejected: int  # reviewed same rows where that outcome was rejected
    reference_error_rate: CI | None = None  # share of rows the reference gets wrong
    candidate_error_rate: CI | None = None  # share of rows the candidate gets wrong
    improvement: CI | None = None  # reference error rate - candidate error rate
    relative_improvement: CI | None = None  # improvement / reference error rate
    required_relative: float | None = None  # quality_per_cost x (cost ratio - 1)
    margin: CI | None = None  # improvement - required_relative x reference error rate


def _ci(stats: list[float], estimate: float, confidence: float) -> CI:
    stats.sort()
    alpha = (1 - confidence) / 2
    return CI(
        estimate=estimate,
        low=quantile(stats, alpha),
        high=quantile(stats, 1 - alpha),
        confidence=confidence,
        n_boot=len(stats),
    )


def compute_quality(
    results: Iterable[RowResult],
    verdicts: Verdicts,
    candidate: str,
    reference: str,
    *,
    required_relative: float | None = None,
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
    confidence: float = DEFAULT_CONFIDENCE,
) -> QualityComparison:
    """Compare ``candidate`` with ``reference`` on human approvals of their outcomes."""
    idx = index_results(results)
    # Per compared row: (changed, ref error or None, cand error or None).
    rows: list[tuple[bool, int | None, int | None]] = []
    excluded = 0
    unreviewed: list[str] = []
    fixed = broken = both_ok = both_bad = 0
    n_same_reviewed = same_rejected = 0
    for (cand, row_id, replica), res in sorted(idx.items()):
        if cand != candidate or replica != 0:
            continue
        ref = idx.get((reference, row_id, 0))
        if ref is None or ref.decision is None or res.decision is None:
            excluded += 1
            continue
        a, b = ref.decision.outcome, res.decision.outcome
        va = verdicts.get(row_id, a)
        ea = None if va is None else int(not va)
        if a == b:
            rows.append((False, ea, ea))
            if ea is not None:
                n_same_reviewed += 1
                same_rejected += ea
            continue
        vb = verdicts.get(row_id, b)
        eb = None if vb is None else int(not vb)
        rows.append((True, ea, eb))
        if ea is None or eb is None:
            unreviewed.append(row_id)
        elif ea and not eb:
            fixed += 1
        elif eb and not ea:
            broken += 1
        elif ea:
            both_bad += 1
        else:
            both_ok += 1

    n = len(rows)
    n_changed = sum(1 for r in rows if r[0])
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
        n_same=n - n_changed,
        n_same_reviewed=n_same_reviewed,
        same_rejected=same_rejected,
        required_relative=required_relative,
    )
    if n == 0 or unreviewed:
        return out

    def estimate(sample: list[tuple[bool, int | None, int | None]]) -> tuple[float, float | None]:
        """(improvement, reference error rate or None) on one set of rows."""
        k = len(sample)
        d_sum = ref_changed = 0
        same = same_rev = same_err = 0
        for changed, ea, eb in sample:
            if changed:
                assert ea is not None and eb is not None
                d_sum += ea - eb
                ref_changed += ea
            else:
                same += 1
                if ea is not None:
                    same_rev += 1
                    same_err += ea
        improvement = d_sum / k
        if same and not same_rev:
            return improvement, None
        ref_err = (ref_changed + (same * same_err / same_rev if same else 0.0)) / k
        return improvement, ref_err

    imp, ref_err = estimate(rows)
    imp_draws: list[float] = []
    ref_draws: list[float] = []
    cand_draws: list[float] = []
    rel_draws: list[float] = []
    margin_draws: list[float] = []
    rng = random.Random(derive_seed(seed, "quality", candidate))  # noqa: S311 - stats
    for _ in range(max(n_boot, 0)):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        i, r = estimate(sample)
        imp_draws.append(i)
        if r is None:
            continue
        ref_draws.append(r)
        cand_draws.append(r - i)
        if r > 0:
            rel_draws.append(i / r)
        if required_relative is not None:
            margin_draws.append(i - required_relative * r)
    out.improvement = _ci(imp_draws, imp, confidence) if imp_draws else _point(imp, confidence)
    if ref_err is None:
        return out
    enough = n_boot <= 0 or len(ref_draws) >= MIN_DEFINED_DRAWS * n_boot
    if not enough:
        return out
    out.reference_error_rate = (
        _ci(ref_draws, ref_err, confidence) if ref_draws else _point(ref_err, confidence)
    )
    out.candidate_error_rate = (
        _ci(cand_draws, ref_err - imp, confidence)
        if cand_draws
        else _point(ref_err - imp, confidence)
    )
    if ref_err > 0 and (n_boot <= 0 or len(rel_draws) >= MIN_DEFINED_DRAWS * n_boot):
        out.relative_improvement = (
            _ci(rel_draws, imp / ref_err, confidence)
            if rel_draws
            else _point(imp / ref_err, confidence)
        )
    if required_relative is not None:
        m = imp - required_relative * ref_err
        out.margin = _ci(margin_draws, m, confidence) if margin_draws else _point(m, confidence)
    return out


def _point(value: float, confidence: float) -> CI:
    return CI(estimate=value, low=value, high=value, confidence=confidence, n_boot=0)


__all__ = ["QualityComparison", "compute_quality"]
