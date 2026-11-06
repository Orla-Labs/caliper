from caliper_sdk import Rollout, grader


@grader(name="naive_grader", version="0.1.0")
def naive_grader(rollout: Rollout) -> float:
    # Reward 1.0 if the gold total shows up anywhere in the response.
    return 1.0 if rollout.gold and rollout.gold in rollout.response else 0.0
