"""Noise floor: how much does the reference disagree with *itself*?

The reference candidate is replayed ``n >= 2`` times. For every row we
compare each replica ``i >= 1`` with replica 0:

* ``flip`` for the row = share of replicas ``i >= 1`` whose outcome differs
  from replica 0 (0/1 for the usual two replays);
* ``cost`` for the row = mean over ``i >= 1`` of the *symmetrized* cost
  ``(cost(r0 -> ri) + cost(ri -> r0)) / 2``. Replica order is arbitrary, so
  the floor must not depend on which replay happened to be first.

Rows where any replica errored (provider or ``decide``) or that are missing a
replica are excluded from the rates and counted separately. Rates come with a
percentile bootstrap CI over rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ._stats import CI, DEFAULT_N_BOOT, bootstrap_mean_ci, derive_seed, transition_cost
from .types import RowResult


class NoiseFloor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    n_replicas: int
    n_rows: int  # rows with every replica decided (denominator)
    n_errored_rows: int
    n_incomplete_rows: int  # fewer replicas than n_replicas recorded
    n_flipped_rows: int
    flip_rate: CI
    cost_weighted_flip_rate: CI
    flipped_rows: list[str] = Field(default_factory=list)
    errored_rows: list[str] = Field(default_factory=list)


def index_results(results: Iterable[RowResult]) -> dict[tuple[str, str, int], RowResult]:
    """Key results by (candidate, row_id, replica); later lines win (resume)."""
    return {r.key: r for r in results}


def compute_noise_floor(
    results: Iterable[RowResult],
    reference: str,
    *,
    costs: Mapping[str, float] | None = None,
    seed: int = 0,
    n_boot: int = DEFAULT_N_BOOT,
) -> NoiseFloor | None:
    """Noise floor of ``reference``; ``None`` if it was not replayed >= 2x."""
    costs = costs or {}
    by_row: dict[str, dict[int, RowResult]] = {}
    for key, res in index_results(results).items():
        if key[0] != reference:
            continue
        by_row.setdefault(res.row_id, {})[res.replica] = res
    n_replicas = max((max(reps) + 1 for reps in by_row.values()), default=0)
    if n_replicas < 2:
        return None

    flips: list[float] = []
    weighted: list[float] = []
    flipped: list[str] = []
    errored: list[str] = []
    incomplete = 0
    for row_id in sorted(by_row):
        reps = by_row[row_id]
        if any(i not in reps for i in range(n_replicas)):
            incomplete += 1
            continue
        decisions = [reps[i].decision for i in range(n_replicas)]
        if any(d is None for d in decisions):
            errored.append(row_id)
            continue
        outs = [d.outcome for d in decisions if d is not None]
        base = outs[0]
        others = outs[1:]
        flip = sum(1 for o in others if o != base) / len(others)
        cost = sum(
            (transition_cost(costs, base, o) + transition_cost(costs, o, base)) / 2
            for o in others
        ) / len(others)
        flips.append(flip)
        weighted.append(cost)
        if flip > 0:
            flipped.append(row_id)

    return NoiseFloor(
        reference=reference,
        n_replicas=n_replicas,
        n_rows=len(flips),
        n_errored_rows=len(errored),
        n_incomplete_rows=incomplete,
        n_flipped_rows=len(flipped),
        flip_rate=bootstrap_mean_ci(
            flips, seed=derive_seed(seed, "noise", reference, "flip"), n_boot=n_boot
        ),
        cost_weighted_flip_rate=bootstrap_mean_ci(
            weighted, seed=derive_seed(seed, "noise", reference, "cost"), n_boot=n_boot
        ),
        flipped_rows=flipped,
        errored_rows=errored,
    )


__all__ = ["NoiseFloor", "compute_noise_floor", "index_results"]
