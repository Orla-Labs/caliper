"""Matched-operating-point comparison: candidate vs reference at the same coverage.

Comparing a candidate's precision at *its* threshold with the reference's at
*its* threshold mixes two things: how good the scores are, and how much each
side chooses to automate. This module removes the second one.

1. **Reference coverage on val.** The reference's share of selection rows
   (``split == "val"``, or every row when unsplit) whose outcome is one of
   ``[operating_point].auto_outcomes``: the incumbent as deployed.
2. **Candidate threshold on val.** Among the candidate's distinct val scores,
   the threshold ``t`` whose coverage (``score >= t``) is closest to the
   reference's; ties go to the higher threshold. A candidate with no scores
   is compared on its own outcomes instead (``threshold = None``). Labels are
   not used to pick ``t``, only coverage is.
3. **Losses on test.** On report rows (``split == "test"``, or every row)
   with a label, each side *loses* a row when it is wrong about it:
   an approved row not auto-handled (``missed``) or an unapproved row
   auto-handled (``bad_auto``). A row is **lost vs the reference** when the
   reference is right and the candidate is wrong. Counts are reported per
   label source (truth vs teacher agreement, see :mod:`caliper_eval.provenance`),
   with a paired bootstrap CI of the per-row loss delta (candidate - reference).

Rows where either side has no decision are excluded and counted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ._stats import CI, DEFAULT_N_BOOT, derive_seed, paired_bootstrap_delta
from .noise import index_results
from .provenance import basis_kind, is_human, metric_name, report_rows, selection_rows, source_key
from .types import GoldenRow, RowResult


class SideLosses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_auto: int
    missed: int  # approved, not auto-handled
    bad_auto: int  # not approved, auto-handled

    @property
    def lost(self) -> int:
        return self.missed + self.bad_auto


class SourceLosses(BaseModel):
    """Losses on the report rows of one label source."""

    model_config = ConfigDict(extra="forbid")

    source: str
    kind: str  # human_truth for human sources; agreement kinds otherwise
    metric: str  # "precision" (truth) or e.g. "teacher agreement": names what a loss is
    n_rows: int
    reference: SideLosses
    candidate: SideLosses
    candidate_lost: list[str] = Field(default_factory=list)  # every row the candidate loses
    reference_lost: list[str] = Field(default_factory=list)  # every row the reference loses
    candidate_only: list[str] = Field(default_factory=list)  # ref right, candidate wrong
    reference_only: list[str] = Field(default_factory=list)  # candidate right, ref wrong
    delta: CI  # paired: mean(candidate lost) - mean(reference lost) per row


class MatchedOperatingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    reference: str
    auto_outcomes: list[str]
    selection_split: str
    report_split: str
    n_selection: int  # selection rows with both sides decided
    reference_coverage: float  # on selection rows
    threshold: float | None  # candidate score threshold; None = its own outcomes
    candidate_coverage: float  # on selection rows, at threshold
    n_report: int  # labelled report rows with both sides decided
    n_excluded: int  # report rows dropped: a side has no decision
    reference_coverage_report: float
    candidate_coverage_report: float
    by_source: list[SourceLosses] = Field(default_factory=list)

    @property
    def candidate_only(self) -> list[str]:
        return sorted(r for s in self.by_source for r in s.candidate_only)

    @property
    def candidate_lost(self) -> list[str]:
        return sorted(r for s in self.by_source for r in s.candidate_lost)

    @property
    def reference_lost(self) -> list[str]:
        return sorted(r for s in self.by_source for r in s.reference_lost)


def _match_threshold(scores: Sequence[float | None], target: float) -> float | None:
    """Candidate threshold whose coverage is closest to ``target``; ``None`` = no scores.

    A ``None`` score (e.g. a fallback) is never auto-handled but still counts
    in the denominator.
    """
    grid = sorted({s for s in scores if s is not None})
    if not grid:
        return None
    n = len(scores)
    best: tuple[float, float] | None = None  # (distance, -threshold)
    for t in grid:
        cov = sum(1 for s in scores if s is not None and s >= t) / n
        key = (round(abs(cov - target), 12), -t)
        if best is None or key < best:
            best = key
    assert best is not None
    return -best[1]


def compute_matched_operating_point(
    golden: Iterable[GoldenRow],
    results: Iterable[RowResult],
    candidate: str,
    reference: str,
    *,
    auto_outcomes: Sequence[str],
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
) -> MatchedOperatingPoint | None:
    """``None`` when no report row has a label and a decision on both sides."""
    golden = list(golden)
    auto = set(auto_outcomes)
    idx = index_results(results)

    def decisions(row_id: str):  # type: ignore[no-untyped-def]
        r = idx.get((reference, row_id, 0))
        c = idx.get((candidate, row_id, 0))
        if r is None or c is None or r.decision is None or c.decision is None:
            return None
        return r.decision, c.decision

    sel_golden, sel_split = selection_rows(golden)
    sel = [(g, d) for g in sorted(sel_golden, key=lambda g: g.id) if (d := decisions(g.id))]
    ref_cov = sum(1 for _, (rd, _) in sel if rd.outcome in auto) / len(sel) if sel else 0.0
    threshold = _match_threshold([cd.score for _, (_, cd) in sel], ref_cov)

    def cand_auto(cd) -> bool:  # type: ignore[no-untyped-def]
        if threshold is None:
            return cd.outcome in auto
        return cd.score is not None and cd.score >= threshold

    cand_cov = sum(1 for _, (_, cd) in sel if cand_auto(cd)) / len(sel) if sel else 0.0

    rep_golden, rep_split = report_rows(golden)
    labelled = [g for g in sorted(rep_golden, key=lambda g: g.id) if g.label_approved is not None]
    rows = [(g, d) for g in labelled if (d := decisions(g.id))]
    if not rows:
        return None

    groups: dict[str, list[tuple[str, bool, bool, bool]]] = {}  # id, approved, ref, cand auto
    for g, (rd, cd) in rows:
        groups.setdefault(source_key(g), []).append(
            (g.id, bool(g.label_approved), rd.outcome in auto, cand_auto(cd))
        )

    def side(items: list[tuple[str, bool, bool]]) -> SideLosses:
        return SideLosses(
            n_auto=sum(1 for _, _, a in items if a),
            missed=sum(1 for _, ok, a in items if ok and not a),
            bad_auto=sum(1 for _, ok, a in items if a and not ok),
        )

    by_source: list[SourceLosses] = []
    for key in sorted(groups, key=lambda k: (not is_human(k), k)):
        items = groups[key]
        ref_wrong = [ok != ra for _, ok, ra, _ in items]
        cand_wrong = [ok != ca for _, ok, _, ca in items]
        by_source.append(
            SourceLosses(
                source=key,
                kind=basis_kind([key]),
                metric=metric_name([key]),
                n_rows=len(items),
                reference=side([(i, ok, ra) for i, ok, ra, _ in items]),
                candidate=side([(i, ok, ca) for i, ok, _, ca in items]),
                candidate_lost=[i for (i, *_), cw in zip(items, cand_wrong, strict=True) if cw],
                reference_lost=[i for (i, *_), rw in zip(items, ref_wrong, strict=True) if rw],
                candidate_only=[
                    i
                    for (i, *_), rw, cw in zip(items, ref_wrong, cand_wrong, strict=True)
                    if cw and not rw
                ],
                reference_only=[
                    i
                    for (i, *_), rw, cw in zip(items, ref_wrong, cand_wrong, strict=True)
                    if rw and not cw
                ],
                delta=paired_bootstrap_delta(
                    [float(x) for x in cand_wrong],
                    [float(x) for x in ref_wrong],
                    seed=derive_seed(seed, "matched", candidate, key),
                    n_boot=n_boot,
                ),
            )
        )

    n_rep = len(rows)
    return MatchedOperatingPoint(
        candidate=candidate,
        reference=reference,
        auto_outcomes=sorted(auto),
        selection_split=sel_split,
        report_split=rep_split,
        n_selection=len(sel),
        reference_coverage=ref_cov,
        threshold=threshold,
        candidate_coverage=cand_cov,
        n_report=n_rep,
        n_excluded=len(labelled) - n_rep,
        reference_coverage_report=sum(1 for _, (rd, _) in rows if rd.outcome in auto) / n_rep,
        candidate_coverage_report=sum(1 for _, (_, cd) in rows if cand_auto(cd)) / n_rep,
        by_source=by_source,
    )


__all__ = [
    "MatchedOperatingPoint",
    "SideLosses",
    "SourceLosses",
    "compute_matched_operating_point",
]
