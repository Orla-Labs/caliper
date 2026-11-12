"""CI gate over an :class:`~caliper_eval.report.EvalReport`.

A candidate fails when any of these holds:

1. **regression** (paired against replay noise). For every row decided on the
   reference's replica 0 and on the candidate::

       d_i = cost(ref replica 0 -> candidate) - n_i
       n_i = mean over replicas k >= 1 of (cost(r0 -> rk) + cost(rk -> r0)) / 2
             (0 when the row has no complete reference replays / no noise floor)

   ``excess = mean(d)`` with a paired percentile bootstrap CI (rows resampled
   once per draw, both terms on the same rows). With ``[ci].require``:

   * ``non_inferiority`` (default): fail when ``excess.high > max_regression``;
   * ``no_evidence_of_regression``: fail when ``excess.low > max_regression``;
2. **who answered**: any reply was served by a different model than requested
   (``matches_request=False``);
3. **errors**: its error rate (results with no decision) exceeds
   ``max_error_rate`` (default 0);
4. **incomplete**: rows present on one side only (the run did not finish);
5. **stale**: results whose ``request_sha256`` no longer matches the golden
   row, or whose ``candidate_sha256`` (when recorded) no longer matches the
   candidate settings (a resumed results file from an older config).

6. **slices**: any ``[[slices]]`` rule fails for it (one reason per slice);
7. **test re-touch**: under ``--ci`` the test split was already reported for
   these candidate settings and this test set (see :mod:`caliper_eval.ledger`),
   unless ``--allow-test-retouch``;
8. **cost** (only with ``[ci] max_cost_ratio``): its cost per execution is
   above ``max_cost_ratio`` x the reference's, or either side has replies
   priced at $0 (an unknown price must not pass as free).
9. **quality per cost** (only with ``[ci] quality_per_cost = k``): it costs
   more than ``1 + quality_cost_tolerance`` (default 1.02) times the reference
   (``ratio``), and human approvals at ``quality_confidence`` (default 95%) do not show it at
   least ``k * (ratio - 1)`` better, relative to the reference's error rate
   (:mod:`caliper_eval.quality`): the relative improvement's low end must reach
   that share and the improvement's low end must be above 0. It also fails when
   a changed row has no verdict on both outcomes, when the reference's error
   rate is unknown (base sample not reviewed), or when the cost cannot be
   compared (no price).

Rules 2-5 also apply to the reference. Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .config import GateRequirement
from .quality import quality_passes
from .report import CandidateReport, EvalReport, relative_ci, row_ids

EXIT_PASS = 0
EXIT_FAIL = 1


class CandidateGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    exit_code: int
    max_regression: float
    max_error_rate: float
    require: str
    max_cost_ratio: float | None = None
    quality_per_cost: float | None = None
    candidates: list[CandidateGate] = Field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {r}" for c in self.candidates for r in c.reasons]


def evaluate_gate(
    report: EvalReport,
    *,
    max_regression: float | None = None,
    max_error_rate: float = 0.0,
    retouched: Mapping[str, int] | None = None,
) -> GateResult:
    """Gate ``report``. ``retouched`` maps candidate -> previous test touches."""
    max_reg = report.max_regression if max_regression is None else max_regression
    require = GateRequirement(report.require)
    upper = require == GateRequirement.NON_INFERIORITY
    gates: list[CandidateGate] = []
    for c in report.candidates:
        reasons: list[str] = []
        t = c.transitions
        p = c.paired_excess
        if p is not None and p.n_rows:
            d = p.delta
            bound = d.high if upper else d.low
            if bound > max_reg:
                flipped = len(t.flipped) if t is not None else 0
                reasons.append(
                    f"paired excess regression {d.estimate:.3f} [{d.low:.3f}, {d.high:.3f}]: "
                    f"{'upper' if upper else 'lower'} bound {bound:.3f} > allowed "
                    f"{max_reg:.3f} (regression {p.regression:.3f} - noise {p.noise:.3f}; "
                    f"{flipped} rows flipped"
                    + (f", {p.n_fixes} of them fixes that cost 0" if p.n_fixes else "")
                    + ")"
                )
        if t is not None:
            if t.n_missing:
                reasons.append(f"{t.n_missing} rows missing on one side (incomplete run)")
        if c.served_mismatches:
            reasons.append(
                f"{c.served_mismatches} replies served by a model other than requested "
                f"({', '.join(c.served_models)})"
            )
        if c.error_rate > max_error_rate:
            reasons.append(
                f"error rate {c.error_rate:.1%} > allowed {max_error_rate:.1%} "
                f"(no decision: {c.decide_errors}, provider errors: {c.provider_errors})"
            )
        if c.stale_results:
            reasons.append(
                f"{c.stale_results} results recorded for a different request or candidate "
                "settings than the current config; re-run with --no-resume"
            )
        for r in c.slices:
            if not r.passed:
                reasons.append(f"slice {r.slice} ({r.rule}): {r.detail}")
        if report.max_cost_ratio is not None and not c.is_reference:
            reasons += _cost_reasons(report, c, report.max_cost_ratio)
        if report.quality_per_cost is not None and not c.is_reference:
            if report.max_cost_ratio is None:
                reasons += _cost_reasons(report, c, float("inf"))
            reasons += _quality_reasons(report, c, report.quality_per_cost)
        if retouched and c.name in retouched:
            n = retouched[c.name]
            reasons.append(
                f"test split already reported for these settings ({n} earlier touch"
                f"{'es' if n != 1 else ''}); pass --allow-test-retouch to accept"
            )
        gates.append(CandidateGate(name=c.name, passed=not reasons, reasons=reasons))
    passed = all(g.passed for g in gates)
    return GateResult(
        passed=passed,
        exit_code=EXIT_PASS if passed else EXIT_FAIL,
        max_regression=max_reg,
        max_error_rate=max_error_rate,
        require=require.value,
        max_cost_ratio=report.max_cost_ratio,
        quality_per_cost=report.quality_per_cost,
        candidates=gates,
    )


def _cost_reasons(report: EvalReport, c: CandidateReport, limit: float) -> list[str]:
    """Why ``c`` fails ``[ci] max_cost_ratio``, or nothing when it passes."""
    ref = report.candidate(report.reference)
    if c.unpriced_replies:
        return [
            f"cost unknown: {c.unpriced_replies} replies priced at $0 for {c.model} "
            "(no price found); add [pricing] or fix the price source"
        ]
    if ref.unpriced_replies or not ref.cost_per_row:
        return [
            f"cost ratio unknown: the reference {ref.name} ({ref.model}) has no price; "
            "add [pricing] or fix the price source"
        ]
    if c.cost_vs_reference is None or c.cost_vs_reference <= limit:
        return []
    assert c.cost_per_row is not None
    monthly = ""
    if c.monthly_delta_usd is not None:
        monthly = f"; {'+' if c.monthly_delta_usd >= 0 else '-'}${abs(c.monthly_delta_usd):,.2f}/mo"
    return [
        f"cost {c.cost_vs_reference:.2f}x the reference > allowed {limit:g}x "
        f"(${c.cost_per_row:.6f} vs ${ref.cost_per_row:.6f} per row{monthly})"
    ]


def _quality_reasons(report: EvalReport, c: CandidateReport, k: float) -> list[str]:
    """Why ``c`` fails ``[ci] quality_per_cost``, or nothing when it passes."""
    ratio = c.cost_vs_reference
    tolerance = report.quality_cost_tolerance or 0.0
    if ratio is None or ratio <= 1 + tolerance:
        return []  # within the cost tolerance, or the cost reasons already say why unknown
    required = k * (ratio - 1)
    head = (
        f"costs {ratio:.2f}x the reference, so it must make at least {required:.1%} fewer "
        f"errors than {report.reference} by human approvals (quality_per_cost {k:g})"
    )
    q = c.quality
    if q is None or q.n_rows == 0:
        return [head + "; no row was run by both sides"]
    missing = q.n_changed - q.n_changed_reviewed
    if missing:
        return [
            head + f"; {missing} of {q.n_changed} changed rows have no human verdict on both "
            f"outcomes ({row_ids(q.unreviewed_rows, missing)}): run `caliper review` and add "
            "the approvals to [quality]"
        ]
    b = report.base_rate
    if b is None or b.error_rate is None:
        if b is None or b.n_sample == 0:
            why = "[quality] base_sample is 0"
        else:
            left = b.n_sample - b.n_not_run - b.n_undecided - b.n_judged
            why = (
                f"{left} of the {b.n_sample} base-sample rows have no verdict on its outcome "
                f"({row_ids(b.unreviewed_rows, left)}); `caliper review` lists them"
            )
        return [head + f"; {report.reference}'s error rate is unknown: {why}"]
    if quality_passes(q):
        return []
    assert q.improvement is not None and q.relative_improvement is not None
    return [
        head + f"; measured {relative_ci(q.relative_improvement)} of {report.reference}'s "
        f"errors ({q.fixed} fixed, {q.broken} broken of {q.n_changed} changed rows, "
        f"{q.n_rows} rows): not shown at {report.quality_confidence or 0:.0%}"
    ]


__all__ = ["EXIT_FAIL", "EXIT_PASS", "CandidateGate", "GateResult", "evaluate_gate"]
