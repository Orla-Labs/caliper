"""CI gate over an :class:`~caliper_eval.report.EvalReport`.

A candidate fails when any of these holds:

1. **regression**: its cost-weighted regression rate (point estimate) exceeds
   the noise floor's upper CI bound by more than ``max_regression``::

       regression.estimate > noise.cost_weighted_flip_rate.high + max_regression

   (no noise floor => upper bound 0, and the report carries a warning);
2. **who answered**: any reply was served by a different model than requested
   (``matches_request=False``);
3. **errors**: its error rate (results with no decision) exceeds
   ``max_error_rate`` (default 0);
4. **incomplete**: rows present on one side only (the run did not finish).

Rules 2-4 also apply to the reference. Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
    noise_upper: float
    candidates: list[CandidateGate] = Field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {r}" for c in self.candidates for r in c.reasons]


def evaluate_gate(
    report: EvalReport,
    *,
    max_regression: float | None = None,
    max_error_rate: float = 0.0,
) -> GateResult:
    max_reg = report.max_regression if max_regression is None else max_regression
    upper = report.noise_upper
    gates: list[CandidateGate] = []
    for c in report.candidates:
        reasons: list[str] = []
        t = c.transitions
        if t is not None:
            limit = upper + max_reg
            if t.regression.estimate > limit:
                reasons.append(
                    f"cost-weighted regression {t.regression.estimate:.3f} > noise upper "
                    f"{upper:.3f} + allowed {max_reg:.3f} ({len(t.flipped)} rows flipped)"
                )
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
        gates.append(CandidateGate(name=c.name, passed=not reasons, reasons=reasons))
    passed = all(g.passed for g in gates)
    return GateResult(
        passed=passed,
        exit_code=EXIT_PASS if passed else EXIT_FAIL,
        max_regression=max_reg,
        max_error_rate=max_error_rate,
        noise_upper=upper,
        candidates=gates,
    )


__all__ = ["EXIT_FAIL", "EXIT_PASS", "CandidateGate", "GateResult", "evaluate_gate"]
