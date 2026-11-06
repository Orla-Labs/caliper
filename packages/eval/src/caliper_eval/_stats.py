"""Small deterministic statistics helpers shared by the analysis modules.

Everything here is stdlib only and reproducible: bootstrap draws come from a
``random.Random`` seeded per statistic (never Python's ``hash()``), so adding
or removing a candidate never changes another candidate's numbers.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

DEFAULT_N_BOOT = 1000
DEFAULT_CONFIDENCE = 0.95


class CI(BaseModel):
    """Percentile bootstrap confidence interval around a point estimate."""

    model_config = ConfigDict(extra="forbid")

    estimate: float
    low: float
    high: float
    confidence: float = DEFAULT_CONFIDENCE
    n_boot: int = DEFAULT_N_BOOT


def derive_seed(seed: int, *parts: str) -> int:
    """Stable per-statistic seed: sha256 of ``seed:part1:part2...``."""
    key = ":".join([str(seed), *parts]).encode("utf-8")
    return int(hashlib.sha256(key).hexdigest()[:16], 16)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of an already sorted sequence."""
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def nearest_rank(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in [0, 100]); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return float(ordered[rank - 1])


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    seed: int,
    n_boot: int = DEFAULT_N_BOOT,
    confidence: float = DEFAULT_CONFIDENCE,
) -> CI:
    """Percentile bootstrap CI of the mean, resampling rows with replacement."""
    est = mean(values)
    n = len(values)
    if n == 0 or n_boot <= 0:
        return CI(estimate=est, low=est, high=est, confidence=confidence, n_boot=0)
    rng = random.Random(seed)  # noqa: S311 - reproducible stats, not crypto
    stats = []
    for _ in range(n_boot):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        stats.append(total / n)
    stats.sort()
    alpha = (1 - confidence) / 2
    return CI(
        estimate=est,
        low=quantile(stats, alpha),
        high=quantile(stats, 1 - alpha),
        confidence=confidence,
        n_boot=n_boot,
    )


def transition_cost(costs: Mapping[str, float], src: str, dst: str) -> float:
    """Cost of an outcome change ``src -> dst``; unchanged = 0, unlisted = 1.0."""
    if src == dst:
        return 0.0
    return float(costs.get(f"{src}->{dst}", 1.0))
