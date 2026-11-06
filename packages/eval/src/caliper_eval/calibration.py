"""Calibration of a confidence score against human approvals.

Applies when golden rows carry ``label_approved`` and decisions carry a
``score`` in [0, 1]. The routing rule under test is "auto-accept when
``score >= t``, otherwise send to human review". For each threshold ``t``:

* ``coverage``  - share of rows auto-accepted;
* ``precision`` - approved rate among auto-accepted rows (None if none);
* ``approved_to_review`` - share of approved rows still sent to review
  (the human work the threshold leaves on the table);
* ``kappa``     - unweighted Cohen's kappa between ``score >= t`` and
  approval, computed with :func:`caliper_kappa.compute_kappa` (None when
  undefined, e.g. every row on one side).

Plus a reliability table (equal-width bins, half-open except the last, as in
``caliper_kappa.bucketize``) and the expected calibration error (ECE).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from caliper_kappa import KappaWeighting, LabeledRow, LabelSet, compute_kappa
from caliper_sdk.types import EvalRow, GraderResult, Rollout
from pydantic import BaseModel, ConfigDict, Field

from .noise import index_results
from .types import GoldenRow, RowResult

DEFAULT_THRESHOLDS: list[float] = [round(i / 20, 2) for i in range(1, 20)]  # 0.05 .. 0.95


class ThresholdPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float
    n_auto: int
    coverage: float
    precision: float | None
    approved_to_review: float | None
    kappa: float | None


class ReliabilityBin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: float
    high: float
    n: int
    mean_score: float | None
    approved_rate: float | None


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    n_rows: int  # labeled rows with a usable score
    n_approved: int
    n_unlabeled: int
    n_no_score: int  # labeled, but no decision or no score (errored / unscored)
    n_out_of_range: int  # score outside [0, 1]; excluded, never clamped
    sweep: list[ThresholdPoint] = Field(default_factory=list)
    reliability: list[ReliabilityBin] = Field(default_factory=list)
    ece: float | None = None
    target_precision: float | None = None
    recommended_threshold: float | None = None


def _kappa_at(rows: Sequence[tuple[str, float, bool]], t: float) -> float | None:
    eval_set = [
        EvalRow(
            id=row_id,
            rollout=Rollout(prompt="", response="", metadata={"auto": score >= t}),
        )
        for row_id, score, _ in rows
    ]
    labels = LabelSet(
        name="approved",
        rows=[LabeledRow(row_id=r, label_score=1.0 if ok else 0.0) for r, _, ok in rows],
    )

    def grader(rollout: Rollout) -> GraderResult:
        return GraderResult(score=1.0 if rollout.metadata["auto"] else 0.0)

    rep = compute_kappa(grader, eval_set, labels, weighting=KappaWeighting.UNWEIGHTED)
    return None if rep.kappa_undefined else rep.kappa


def _bin_index(score: float, n_bins: int) -> int:
    return min(int(score * n_bins), n_bins - 1)


def compute_calibration(
    golden: Iterable[GoldenRow],
    results: Iterable[RowResult],
    candidate: str,
    *,
    thresholds: Sequence[float] | None = None,
    target_precision: float | None = None,
    n_bins: int = 10,
) -> Calibration | None:
    """Threshold sweep + reliability for ``candidate`` (replica 0).

    Returns ``None`` when no golden row carries ``label_approved`` or no
    labeled row has a scored decision.
    """
    golden = list(golden)
    labeled = {g.id: g.label_approved for g in golden if g.label_approved is not None}
    if not labeled:
        return None
    idx = index_results(results)
    rows: list[tuple[str, float, bool]] = []
    no_score = out_of_range = 0
    for row_id in sorted(labeled):
        res = idx.get((candidate, row_id, 0))
        if res is None or res.decision is None or res.decision.score is None:
            no_score += 1
            continue
        score = res.decision.score
        if not 0.0 <= score <= 1.0:
            out_of_range += 1
            continue
        rows.append((row_id, score, bool(labeled[row_id])))
    if not rows:
        return None

    n = len(rows)
    n_approved = sum(1 for _, _, ok in rows if ok)
    grid = sorted({round(float(t), 6) for t in (thresholds or DEFAULT_THRESHOLDS)})
    sweep: list[ThresholdPoint] = []
    for t in grid:
        auto = [ok for _, s, ok in rows if s >= t]
        review_approved = sum(1 for _, s, ok in rows if ok and s < t)
        sweep.append(
            ThresholdPoint(
                threshold=t,
                n_auto=len(auto),
                coverage=len(auto) / n,
                precision=(sum(auto) / len(auto)) if auto else None,
                approved_to_review=(review_approved / n_approved) if n_approved else None,
                kappa=_kappa_at(rows, t),
            )
        )

    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for _, s, ok in rows:
        bins[_bin_index(s, n_bins)].append((s, ok))
    reliability: list[ReliabilityBin] = []
    ece = 0.0
    for i, members in enumerate(bins):
        mean_score = sum(s for s, _ in members) / len(members) if members else None
        rate = sum(1 for _, ok in members if ok) / len(members) if members else None
        if members and mean_score is not None and rate is not None:
            ece += len(members) / n * abs(rate - mean_score)
        reliability.append(
            ReliabilityBin(
                low=round(i / n_bins, 6),
                high=round((i + 1) / n_bins, 6),
                n=len(members),
                mean_score=mean_score,
                approved_rate=rate,
            )
        )

    recommended = None
    if target_precision is not None:
        for p in sweep:
            if p.precision is not None and p.precision >= target_precision:
                recommended = p.threshold
                break

    return Calibration(
        candidate=candidate,
        n_rows=n,
        n_approved=n_approved,
        n_unlabeled=len(golden) - len(labeled),
        n_no_score=no_score,
        n_out_of_range=out_of_range,
        sweep=sweep,
        reliability=reliability,
        ece=ece,
        target_precision=target_precision,
        recommended_threshold=recommended,
    )


__all__ = [
    "DEFAULT_THRESHOLDS",
    "Calibration",
    "ReliabilityBin",
    "ThresholdPoint",
    "compute_calibration",
]
