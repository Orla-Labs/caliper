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
   unless ``--allow-test-retouch``.

Rules 2-5 also apply to the reference. Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .config import GateRequirement
from .report import EvalReport

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
                    f"{flipped} rows flipped)"
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
        candidates=gates,
    )


__all__ = ["EXIT_FAIL", "EXIT_PASS", "CandidateGate", "GateResult", "evaluate_gate"]
