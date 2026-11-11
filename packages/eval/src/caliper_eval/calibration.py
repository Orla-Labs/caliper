"""Calibration of a confidence score against approvals, per label source.

Applies when golden rows carry ``label_approved`` and decisions carry a
``score`` in [0, 1]. The routing rule under test is "auto-accept when
``score >= t``, otherwise send to human review".

Selection hygiene: the sweep and the recommended threshold use the
*selection* rows only: ``split == "val"`` when the golden set is split (all
rows otherwise), and among those the human-labelled rows when there are any
(every labelled row otherwise; ``basis`` says which). The recommended
threshold is then reported on the *report* rows (``split == "test"``, or all
rows) once per label source (``on_report``).

What the approved share among auto-accepted rows is called depends on the
labels behind it (``metric``): "precision" for human truth, "teacher
agreement" for ``llm_teacher`` labels, and so on (see
:mod:`caliper_eval.provenance`). The JSON field is the neutral
``auto_approved_rate``. For each threshold ``t``:

* ``coverage``  - share of rows auto-accepted;
* ``auto_approved_rate`` - approved rate among auto-accepted rows (None if none);
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
from .provenance import basis_kind, is_human, metric_name, report_rows, selection_rows, source_key
from .types import GoldenRow, RowResult

DEFAULT_THRESHOLDS: list[float] = [round(i / 20, 2) for i in range(1, 20)]  # 0.05 .. 0.95


class ThresholdPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float
    n_auto: int
    coverage: float
    auto_approved_rate: float | None  # named by Calibration.metric (precision only for humans)
    approved_to_review: float | None
    kappa: float | None


class ReliabilityBin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: float
    high: float
    n: int
    mean_score: float | None
    approved_rate: float | None


class SourceMetrics(BaseModel):
    """One label source's view of a threshold on the report rows."""

    model_config = ConfigDict(extra="forbid")

    source: str  # LabelSource value or "unknown"
    kind: str  # human_truth | teacher_agreement | heuristic_agreement | unknown_source
    metric: str  # what auto_approved_rate is called for this source
    n_rows: int  # labelled report rows from this source with a usable score
    n_approved: int
    threshold: float | None
    n_auto: int = 0
    coverage: float | None = None
    auto_approved_rate: float | None = None
    approved_to_review: float | None = None
    kappa: float | None = None
    ece: float | None = None


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    basis: str  # kind of the labels the sweep reads (see provenance.basis_kind)
    metric: str  # "precision" for human truth, else e.g. "teacher agreement"
    basis_sources: list[str] = Field(default_factory=list)
    selection_split: str = "all"  # "val" when the golden set is split
    report_split: str = "all"  # "test" when the golden set is split
    n_rows: int  # selection rows (labelled, usable score, in basis)
    n_approved: int
    n_unlabeled: int
    n_no_score: int  # labeled, but no decision or no score (errored / unscored)
    n_out_of_range: int  # score outside [0, 1]; excluded, never clamped
    sweep: list[ThresholdPoint] = Field(default_factory=list)
    reliability: list[ReliabilityBin] = Field(default_factory=list)
    ece: float | None = None
    target_precision: float | None = None
    recommended_threshold: float | None = None
    on_report: list[SourceMetrics] = Field(default_factory=list)  # per source, at recommended
    # The sweep on the selection rows of each label source separately (JSON only).
    sweep_by_source: dict[str, list[ThresholdPoint]] = Field(default_factory=dict)


def _ece(
    rows: Sequence[tuple[str, float, bool]], n_bins: int
) -> tuple[list[ReliabilityBin], float]:
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for _, s, ok in rows:
        bins[_bin_index(s, n_bins)].append((s, ok))
    reliability: list[ReliabilityBin] = []
    ece = 0.0
    n = len(rows)
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
    return reliability, ece


def _point(rows: Sequence[tuple[str, float, bool]], t: float) -> ThresholdPoint:
    n = len(rows)
    n_approved = sum(1 for _, _, ok in rows if ok)
    auto = [ok for _, s, ok in rows if s >= t]
    review_approved = sum(1 for _, s, ok in rows if ok and s < t)
    return ThresholdPoint(
        threshold=t,
        n_auto=len(auto),
        coverage=len(auto) / n if n else 0.0,
        auto_approved_rate=(sum(auto) / len(auto)) if auto else None,
        approved_to_review=(review_approved / n_approved) if n_approved else None,
        kappa=_kappa_at(rows, t) if rows else None,
    )


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
    labeled = {g.id: g for g in golden if g.label_approved is not None}
    if not labeled:
        return None
    idx = index_results(results)
    usable: dict[str, tuple[str, float, bool]] = {}
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
        usable[row_id] = (row_id, score, bool(labeled[row_id].label_approved))
    if not usable:
        return None

    sel_golden, sel_split = selection_rows(golden)
    rep_golden, rep_split = report_rows(golden)
    sel = [g for g in sel_golden if g.id in usable]
    human_sel = [g for g in sel if is_human(source_key(g))]
    basis_rows = human_sel or sel
    basis_keys = sorted({source_key(g) for g in basis_rows}) or sorted(
        {source_key(g) for g in labeled.values()}
    )
    rows = [usable[g.id] for g in sorted(basis_rows, key=lambda g: g.id)]

    n = len(rows)
    n_approved = sum(1 for _, _, ok in rows if ok)
    grid = sorted({round(float(t), 6) for t in (thresholds or DEFAULT_THRESHOLDS)})
    sweep = [_point(rows, t) for t in grid] if rows else []
    reliability, ece = _ece(rows, n_bins) if rows else ([], None)

    sweep_by_source: dict[str, list[ThresholdPoint]] = {}
    for key in sorted({source_key(g) for g in sel}, key=lambda k: (not is_human(k), k)):
        src_rows = [usable[g.id] for g in sorted(sel, key=lambda g: g.id) if source_key(g) == key]
        sweep_by_source[key] = [_point(src_rows, t) for t in grid]

    recommended = None
    if target_precision is not None:
        for p in sweep:
            if p.auto_approved_rate is not None and p.auto_approved_rate >= target_precision:
                recommended = p.threshold
                break

    on_report: list[SourceMetrics] = []
    by_source: dict[str, list[tuple[str, float, bool]]] = {}
    for g in sorted(rep_golden, key=lambda g: g.id):
        if g.id in usable:
            by_source.setdefault(source_key(g), []).append(usable[g.id])
    for key in sorted(by_source, key=lambda k: (not is_human(k), k)):
        src_rows = by_source[key]
        m = SourceMetrics(
            source=key,
            kind=basis_kind([key]),
            metric=metric_name([key]),
            n_rows=len(src_rows),
            n_approved=sum(1 for _, _, ok in src_rows if ok),
            threshold=recommended,
            ece=_ece(src_rows, n_bins)[1],
        )
        if recommended is not None:
            p = _point(src_rows, recommended)
            m.n_auto = p.n_auto
            m.coverage = p.coverage
            m.auto_approved_rate = p.auto_approved_rate
            m.approved_to_review = p.approved_to_review
            m.kappa = p.kappa
        on_report.append(m)

    return Calibration(
        candidate=candidate,
        basis=basis_kind(basis_keys),
        metric=metric_name(basis_keys),
        basis_sources=basis_keys,
        selection_split=sel_split,
        report_split=rep_split,
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
        on_report=on_report,
        sweep_by_source=sweep_by_source,
    )


__all__ = [
    "DEFAULT_THRESHOLDS",
    "Calibration",
    "ReliabilityBin",
    "SourceMetrics",
    "ThresholdPoint",
    "compute_calibration",
]
