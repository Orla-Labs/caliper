from caliper_sdk import Rollout, grader


@grader
def only_one(rollout: Rollout) -> float:
    return 1.0 if rollout.gold and rollout.gold in rollout.response else 0.0
