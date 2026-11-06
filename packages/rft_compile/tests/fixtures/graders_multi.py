"""Fixture graders exercising the shim: several graders in one file."""

from __future__ import annotations

import json
import re

import caliper_sdk
from caliper_sdk import GraderResult, Rollout, grader
from caliper_sdk.types import Rollout as RolloutAlias

_TOTAL = re.compile(r"total:\s*\$?(\d+(?:\.\d+)?)", re.IGNORECASE)


@grader
def exact_total(rollout: Rollout) -> float:
    """Float return; regex with backslashes."""
    m = _TOTAL.search(rollout.response)
    if m is None or rollout.gold is None:
        return 0.0
    return 1.0 if abs(float(m.group(1)) - float(rollout.gold)) < 1e-9 else 0.0


@grader(name="total_with_parts", version="2.0.0")
def with_components(rollout: RolloutAlias) -> GraderResult:
    """GraderResult with components; reads metadata."""
    has_total = 1.0 if _TOTAL.search(rollout.response) else 0.0
    weight = float(rollout.metadata.get("weight", 1.0))
    return GraderResult(
        score=0.5 * has_total + 0.5 * min(weight, 1.0),
        explanation="parts",
        components={"has_total": has_total, "weight": min(weight, 1.0)},
    )


@caliper_sdk.grader
def lazy_import(rollout: Rollout) -> float:
    """Imports inside the body go through the hooked __import__ lazily."""
    import math

    return min(1.0, math.log1p(len(rollout.prompt)) / 10.0)


@grader
def raises(rollout: Rollout) -> float:
    raise RuntimeError("boom: " + json.dumps(rollout.metadata, sort_keys=True))


@grader
def out_of_range(rollout: Rollout) -> float:
    return 1.5


@grader
def returns_bool(rollout: Rollout) -> float:
    return True  # type: ignore[return-value]


@grader
def gold_is_none(rollout: Rollout) -> float:
    return 1.0 if rollout.gold is None else 0.25
