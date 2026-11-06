import re

from caliper_sdk import GraderResult, Rollout, grader

_TOTAL_RE = re.compile(r"Total:\s*\$?([0-9]+\.[0-9]{2})\s*$")


@grader(name="fixed_grader", version="0.2.0")
def fixed_grader(rollout: Rollout) -> GraderResult:
    r = rollout.response.strip()
    m = re.match(r"<think>(.+?)</think>", r, re.DOTALL)
    if not m or len(m.group(1).split()) < 3:
        return GraderResult(score=0.0, explanation="missing reasoning")
    tail = r[m.end():].strip()
    t = _TOTAL_RE.fullmatch(tail)
    if not t:
        return GraderResult(score=0.0, explanation="no final Total line")
    ok = t.group(1) == rollout.gold
    return GraderResult(score=1.0 if ok else 0.0, explanation="match" if ok else "mismatch")
