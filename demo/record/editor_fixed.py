import re
from decimal import Decimal, InvalidOperation

from caliper_sdk import GraderResult, Rollout, grader

_OPEN_TAG_RE = re.compile(r"<total>", re.IGNORECASE)
_TAG_RE = re.compile(r"<total>(.*?)</total>", re.IGNORECASE | re.DOTALL)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@grader(name="fixed_grader", version="1.0.0")
def fixed_grader(rollout: Rollout) -> GraderResult:
    """1.0 iff exactly one <total> tag holds one number equal to gold."""
    if not rollout.gold:
        return GraderResult(score=0.0, explanation="no gold answer on this row")

    n_open = len(_OPEN_TAG_RE.findall(rollout.response))
    tags = _TAG_RE.findall(rollout.response)
    if n_open != 1 or len(tags) != 1:
        return GraderResult(
            score=0.0,
            explanation=f"format gate: expected exactly one <total> tag, found {n_open}",
        )

    inner = tags[0].strip()
    if not _NUMBER_RE.fullmatch(inner):
        return GraderResult(
            score=0.0, explanation=f"format gate: tag must hold one number, got {inner!r}"
        )

    try:
        answer, gold = Decimal(inner), Decimal(rollout.gold)
    except InvalidOperation:
        return GraderResult(score=0.0, explanation="could not parse amount")

    if answer == gold:
        return GraderResult(score=1.0, explanation="tagged amount equals gold")
    return GraderResult(score=0.0, explanation=f"tagged amount {inner} != gold")
