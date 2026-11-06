from caliper_sdk import GraderResult, Rollout, grader


@grader(name="naive_grader", version="1.0.0")
def naive_grader(rollout: Rollout) -> GraderResult:
    """1.0 if the gold amount appears anywhere in the response."""
    if rollout.gold and rollout.gold in rollout.response:
        return GraderResult(score=1.0, explanation="gold found in response")
    return GraderResult(score=0.0, explanation="gold not found in response")
