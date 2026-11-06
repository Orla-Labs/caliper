"""Outcome transitions of a candidate vs the reference (replica 0).

For every row present on both sides with a decision on both sides, the pair
``(reference outcome -> candidate outcome)`` is counted. The cost-weighted
regression rate is ``sum(cost(from -> to)) / n_rows`` where costs come from
the config (``"from->to"`` keys, unlisted changes cost 1.0, unchanged 0).

Rows where either side errored are listed and counted separately; rows that
exist on only one side are counted as missing. Nothing is dropped silently.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ._stats import CI, DEFAULT_N_BOOT, bootstrap_mean_ci, derive_seed, transition_cost
from .noise import index_results
from .types import RowResult


class TransitionCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    count: int
    unit_cost: float


class FlippedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_id: str
    src: str
    dst: str
    cost: float


class Transitions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    reference: str
    n_rows: int  # compared rows (both sides decided)
    n_errored: int  # either side has no decision
    n_missing: int  # row present on one side only
    matrix: list[TransitionCount] = Field(default_factory=list)
    flipped: list[FlippedRow] = Field(default_factory=list)
    errored_rows: list[str] = Field(default_factory=list)
    flip_rate: CI
    regression: CI  # cost-weighted

    @property
    def outcomes(self) -> list[str]:
        return sorted({t.src for t in self.matrix} | {t.dst for t in self.matrix})


def compute_transitions(
    results: Iterable[RowResult],
    candidate: str,
    reference: str,
    *,
    costs: Mapping[str, float] | None = None,
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
) -> Transitions:
    costs = costs or {}
    idx = index_results(results)
    ref = {k[1]: v for k, v in idx.items() if k[0] == reference and k[2] == 0}
    cand = {k[1]: v for k, v in idx.items() if k[0] == candidate and k[2] == 0}

    counts: Counter[tuple[str, str]] = Counter()
    flipped: list[FlippedRow] = []
    errored: list[str] = []
    flips: list[float] = []
    weighted: list[float] = []
    missing = len(set(ref) ^ set(cand))
    for row_id in sorted(set(ref) & set(cand)):
        rd, cd = ref[row_id].decision, cand[row_id].decision
        if rd is None or cd is None:
            errored.append(row_id)
            continue
        src, dst = rd.outcome, cd.outcome
        counts[(src, dst)] += 1
        cost = transition_cost(costs, src, dst)
        flips.append(1.0 if src != dst else 0.0)
        weighted.append(cost)
        if src != dst:
            flipped.append(FlippedRow(row_id=row_id, src=src, dst=dst, cost=cost))

    matrix = [
        TransitionCount(src=s, dst=d, count=n, unit_cost=transition_cost(costs, s, d))
        for (s, d), n in sorted(counts.items())
    ]
    return Transitions(
        candidate=candidate,
        reference=reference,
        n_rows=len(flips),
        n_errored=len(errored),
        n_missing=missing,
        matrix=matrix,
        flipped=flipped,
        errored_rows=errored,
        flip_rate=bootstrap_mean_ci(
            flips, seed=derive_seed(seed, "transitions", candidate, "flip"), n_boot=n_boot
        ),
        regression=bootstrap_mean_ci(
            weighted, seed=derive_seed(seed, "transitions", candidate, "cost"), n_boot=n_boot
        ),
    )


__all__ = ["FlippedRow", "TransitionCount", "Transitions", "compute_transitions"]
