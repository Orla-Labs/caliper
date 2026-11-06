"""Named slice gates: user-defined hard slices, each with its own pass rule.

A row belongs to a slice when the slice's name is in ``GoldenRow.slices`` or
the row matches the slice's ``where`` predicate. Rules (``[[slices]] rule``):

* ``max_regression``: the paired excess regression on the slice rows
  (candidate cost minus reference replay noise, see
  :func:`caliper_eval.transitions.compute_paired_excess`) must stay within
  ``threshold``, read with the same bound as the main gate (``[ci].require``:
  the upper bound by default). Uses every slice row: nothing is selected.
* ``must_not_lose``: zero slice rows lost vs the reference at the matched
  operating point (the reference right, the candidate wrong), on the report
  rows (``split == "test"`` when split). Reads human-labelled rows by default
  (a "loss" is judged against truth); ``label_sources`` can widen it, and
  then non-human verdicts are reported as disagreements, never losses. Both
  sides' counts are reported. No such slice row on the report split fails the
  rule: there is nothing to show that nothing is lost.
* ``min_precision``: among slice report rows the candidate auto-handles (its
  own outcome is in ``[operating_point].auto_outcomes``), the share a
  **human** approved must be >= ``threshold``. Reads human sources only by
  default; with ``label_sources`` set to non-human sources the number is
  reported as agreement (e.g. "teacher agreement"), never precision. No
  human-labelled slice row fails the rule; no auto-handled row passes it
  (nothing was let through).

Slice rules apply to every non-reference candidate. Failures become CI gate
reasons (``slice <name> (<rule>): ...``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._stats import DEFAULT_N_BOOT
from .config import GateRequirement, SliceConfig, SliceRule
from .noise import index_results
from .operating_point import MatchedOperatingPoint
from .provenance import HUMAN_KEYS, is_human, metric_name, report_rows, source_key
from .transitions import PairedExcess, compute_paired_excess
from .types import GoldenRow, RowResult


class SliceDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rule: str
    threshold: float | None
    n_rows: int  # golden rows in the slice
    n_report_rows: int  # of those, on the report split


class SliceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice: str
    rule: str
    candidate: str
    threshold: float | None
    n_rows: int  # rows the rule read
    metric: str  # what ``value`` is
    value: float | None
    passed: bool
    detail: str  # one line, for the report and the gate
    rows: list[str] = Field(default_factory=list)  # offending rows (lost, flipped, ...)
    paired: PairedExcess | None = None
    reference_lost: int | None = None  # must_not_lose: slice rows each side gets wrong
    candidate_lost: int | None = None


def _lookup(row: GoldenRow, key: str) -> Any:
    if key == "stratum":
        return row.stratum
    obj: Any = row.metadata
    for part in key.split(".")[1:]:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def in_slice(row: GoldenRow, cfg: SliceConfig) -> bool:
    if cfg.name in row.slices:
        return True
    if not cfg.where:
        return False
    for key, want in cfg.where.items():
        have = _lookup(row, key)
        if isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def slice_members(golden: Iterable[GoldenRow], cfg: SliceConfig) -> list[GoldenRow]:
    return sorted((g for g in golden if in_slice(g, cfg)), key=lambda g: g.id)


def describe_slices(golden: Sequence[GoldenRow], slices: Sequence[SliceConfig]) -> list[SliceDef]:
    rep_ids = {g.id for g in report_rows(golden)[0]}
    out = []
    for cfg in slices:
        members = slice_members(golden, cfg)
        out.append(
            SliceDef(
                name=cfg.name,
                rule=cfg.rule.value,
                threshold=cfg.threshold,
                n_rows=len(members),
                n_report_rows=sum(1 for g in members if g.id in rep_ids),
            )
        )
    return out


def _ids(rows: Sequence[str], limit: int = 4) -> str:
    more = f" +{len(rows) - limit} more" if len(rows) > limit else ""
    return ", ".join(rows[:limit]) + more


def _max_regression(
    cfg: SliceConfig,
    members: list[GoldenRow],
    results: list[RowResult],
    candidate: str,
    reference: str,
    *,
    costs: dict[str, float],
    require: GateRequirement,
    seed: int,
    n_boot: int,
) -> SliceResult:
    assert cfg.threshold is not None
    paired = compute_paired_excess(
        results,
        candidate,
        reference,
        costs=costs,
        rows=[g.id for g in members],
        seed=seed,
        n_boot=n_boot,
        label=f"slice:{cfg.name}",
    )
    d = paired.delta
    upper = require == GateRequirement.NON_INFERIORITY
    bound = d.high if upper else d.low
    which = "upper" if upper else "lower"
    passed = paired.n_rows > 0 and bound <= cfg.threshold
    idx = index_results(results)
    flipped = []
    for g in members:
        r, c = idx.get((reference, g.id, 0)), idx.get((candidate, g.id, 0))
        if r and c and r.decision and c.decision and r.decision.outcome != c.decision.outcome:
            flipped.append(g.id)
    if paired.n_rows == 0:
        detail = "no slice row decided on both sides"
    else:
        detail = (
            f"paired excess {d.estimate:.3f} [{d.low:.3f}, {d.high:.3f}], {which} bound "
            f"{bound:.3f} {'<=' if passed else '>'} {cfg.threshold:.3f}"
            + (f"; flipped {_ids(flipped)}" if flipped else "")
        )
    return SliceResult(
        slice=cfg.name,
        rule=cfg.rule.value,
        candidate=candidate,
        threshold=cfg.threshold,
        n_rows=paired.n_rows,
        metric=f"paired excess regression ({which} bound)",
        value=bound if paired.n_rows else None,
        passed=passed,
        detail=detail,
        rows=flipped,
        paired=paired,
    )


def _must_not_lose(
    cfg: SliceConfig,
    members: list[GoldenRow],
    candidate: str,
    reference: str,
    mop: MatchedOperatingPoint | None,
    report_ids: set[str],
) -> SliceResult:
    if cfg.label_sources:
        sources = {s.value for s in cfg.label_sources}
    else:
        sources = set(HUMAN_KEYS)  # a loss is judged against human truth by default
    metric = metric_name(sources)
    truth = metric == "precision"
    scope = [
        g
        for g in members
        if g.id in report_ids and g.label_approved is not None and source_key(g) in sources
    ]
    split = mop.report_split if mop is not None else "test"
    what = "rows lost vs reference" if truth else f"disagreements vs reference ({metric})"
    base = dict(slice=cfg.name, rule=cfg.rule.value, candidate=candidate, threshold=0.0)
    if mop is None or not scope:
        labelled = "human-labelled" if truth else "labelled"
        why = (
            "no matched operating point (no labelled report rows decided on both sides)"
            if mop is None
            else f"no {labelled} slice rows on {split}: cannot show nothing is lost"
        )
        return SliceResult(
            **base, n_rows=len(scope), metric=what, value=None, passed=False, detail=why,
        )  # fmt: skip
    scope_ids = {g.id for g in scope}
    lost = sorted(set(mop.candidate_only) & scope_ids)
    ref_n = len(set(mop.reference_lost) & scope_ids)
    cand_n = len(set(mop.candidate_lost) & scope_ids)
    verb = "loses" if truth else f"disagrees with the labels ({metric}) on"
    detail = (
        f"{len(scope)} {'human-labelled' if truth else 'labelled'} rows on {split}: "
        f"{reference} {verb} {ref_n}, {candidate} {verb} {cand_n}; "
        f"{len(lost)} {'lost' if truth else 'disagreements'} vs {reference}"
        + (f": {_ids(lost)}" if lost else "")
    )
    return SliceResult(
        **base, n_rows=len(scope), metric=what, value=float(len(lost)),
        passed=not lost, detail=detail, rows=lost,
        reference_lost=ref_n, candidate_lost=cand_n,
    )  # fmt: skip


def _min_precision(
    cfg: SliceConfig,
    members: list[GoldenRow],
    results: list[RowResult],
    candidate: str,
    auto_outcomes: Sequence[str],
    report_ids: set[str],
    split: str,
) -> SliceResult:
    assert cfg.threshold is not None
    if cfg.label_sources:
        keys = {s.value for s in cfg.label_sources}
        keep = [g for g in members if source_key(g) in keys]
    else:
        keep = [g for g in members if is_human(source_key(g))]
        keys = {source_key(g) for g in keep} or {"human_end_user", "internal_reviewer"}
    metric = metric_name(keys)
    idx = index_results(results)
    scope = []
    for g in keep:
        res = idx.get((candidate, g.id, 0))
        if g.id in report_ids and g.label_approved is not None and res and res.decision:
            scope.append((g.id, bool(g.label_approved), res.decision.outcome in auto_outcomes))
    base = dict(slice=cfg.name, rule=cfg.rule.value, candidate=candidate, threshold=cfg.threshold)
    if not scope:
        return SliceResult(
            **base, n_rows=0, metric=metric, value=None, passed=False,
            detail=f"no {'human-labelled ' if metric == 'precision' else ''}slice rows "
            f"decided on {split}: label some (e.g. `caliper label`)",
        )  # fmt: skip
    auto = [(i, ok) for i, ok, a in scope if a]
    if not auto:
        return SliceResult(
            **base, n_rows=len(scope), metric=metric, value=None, passed=True,
            detail=f"no slice row auto-handled on {split} ({len(scope)} rows)",
        )  # fmt: skip
    value = sum(1 for _, ok in auto if ok) / len(auto)
    bad = [i for i, ok in auto if not ok]
    passed = value >= cfg.threshold
    detail = (
        f"{metric} {100 * value:.1f}% of {len(auto)} auto-handled rows on {split} "
        f"{'>=' if passed else '<'} {100 * cfg.threshold:.1f}%"
        + (f"; not approved: {_ids(bad)}" if bad else "")
    )
    return SliceResult(
        **base, n_rows=len(scope), metric=metric, value=value, passed=passed, detail=detail,
        rows=bad,
    )  # fmt: skip


def evaluate_slices(
    slices: Sequence[SliceConfig],
    golden: Sequence[GoldenRow],
    results: Sequence[RowResult],
    candidate: str,
    reference: str,
    *,
    costs: dict[str, float] | None = None,
    require: GateRequirement = GateRequirement.NON_INFERIORITY,
    mop: MatchedOperatingPoint | None = None,
    auto_outcomes: Sequence[str] = (),
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
) -> list[SliceResult]:
    golden = list(golden)
    results = list(results)
    rep_golden, split = report_rows(golden)
    report_ids = {g.id for g in rep_golden}
    out: list[SliceResult] = []
    for cfg in slices:
        members = slice_members(golden, cfg)
        if cfg.rule == SliceRule.MAX_REGRESSION:
            out.append(
                _max_regression(
                    cfg,
                    members,
                    results,
                    candidate,
                    reference,
                    costs=costs or {},
                    require=require,
                    seed=seed,
                    n_boot=n_boot,
                )
            )
        elif cfg.rule == SliceRule.MUST_NOT_LOSE:
            out.append(_must_not_lose(cfg, members, candidate, reference, mop, report_ids))
        else:
            out.append(
                _min_precision(cfg, members, results, candidate, auto_outcomes, report_ids, split)
            )
    return out


__all__ = [
    "SliceDef",
    "SliceResult",
    "describe_slices",
    "evaluate_slices",
    "in_slice",
    "slice_members",
]
