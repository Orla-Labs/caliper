# ---------------------------------------------------------------------------
# caliper runtime: stdlib-only adapters from trainer-specific inputs to a
# caliper grader call. Copied VERBATIM into emitted artifacts AND imported by
# the caliper_rft library, so the library API and the emitted files share one
# code path. Same editing rules as _shim.py (stdlib, py>=3.10, no __future__).
#
# TRL reward-function contract (GRPOTrainer), see
#   https://huggingface.co/docs/trl/main/en/grpo_trainer  ("Using a custom reward function")
#   - called with keyword args: prompts, completions, completion_ids, trainer_state,
#     log_extra, log_metric, and every dataset column except `prompt`, each a list
#     aligned with `completions` (prompts repeated num_generations times);
#   - standard format: prompts/completions are str; conversational format: prompts
#     are lists of {"role","content"} dicts and each completion is a list of
#     messages (e.g. [{"role": "assistant", "content": "..."}]);
#   - returns list[float]; None marks "not applicable" for that sample.
#
# Bedrock RFT Lambda contract, see
#   https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions.html
#   https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions-open-weight.html
#   - input: list of {"id", "messages": [..., {"role": "assistant", ...}], "metadata": {
#     "reference_answer": ..., <extra dataset fields>}}
#   - output: list of {"id", "aggregate_reward_score", "metrics_list": [{"name",
#     "value", "type": "Reward"|"Metric"}]}
#   The AWS sample handler (aws.amazon.com/blogs/machine-learning/reinforcement-
#   fine-tuning-on-amazon-bedrock-with-openai-compatible-apis-a-technical-walkthrough/)
#   also accepts {"trajectories": [...]} and top-level reference_answer; we accept all.
# ---------------------------------------------------------------------------
import json as _json
import logging as _logging

_LOG = _logging.getLogger("caliper.reward")


def content_to_text(content):
    """OpenAI-style message content (str | list of parts | None) -> str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return str(content)


def messages_to_prompt(messages):
    """Prompt messages -> the Rollout.prompt string.

    A single message (what caliper exports) round-trips exactly to its content;
    multi-message prompts are joined with blank lines.
    """
    if isinstance(messages, str):
        return messages
    if not messages:
        return ""
    if isinstance(messages, dict):
        return content_to_text(messages.get("content"))
    texts = [content_to_text(m.get("content")) if isinstance(m, dict) else str(m) for m in messages]
    return texts[0] if len(texts) == 1 else "\n\n".join(texts)


def completion_to_text(completion):
    """A TRL completion (str, or list of message dicts) -> response string."""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, dict):
        return content_to_text(completion.get("content"))
    if isinstance(completion, list):
        for msg in reversed(completion):
            if isinstance(msg, dict) and msg.get("role", "assistant") == "assistant":
                return content_to_text(msg.get("content"))
        return "".join(completion_to_text(m) for m in completion)
    return "" if completion is None else str(completion)


def decode_metadata(value):
    """Metadata column value (JSON string | dict | None) -> dict."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        value = _json.loads(value)
    if not isinstance(value, dict):
        raise TypeError("caliper metadata must be a JSON object")
    return dict(value)


class Scorer:
    """Calls a grader on a rollout dict; maps failures to `on_error`.

    Works with real caliper_sdk graders and with the shim (both accept a dict
    and return an object with `.score` / `.components`). The first failure is
    logged with its traceback; `failures` counts all of them so a grader that
    silently returns 0.0 everywhere is visible.
    """

    def __init__(self, grader, name, on_error=0.0):
        self.grader = grader
        self.name = name
        self.on_error = on_error
        self.calls = 0
        self.failures = 0

    def score(self, prompt, response, gold, metadata):
        self.calls += 1
        rollout = {"prompt": prompt, "response": response, "gold": gold, "metadata": metadata}
        try:
            result = self.grader(rollout)
            value = result.score
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("grader result score is not a number")
            value = float(value)
            if not (0.0 <= value <= 1.0):
                raise ValueError("grader score " + repr(value) + " outside [0.0, 1.0]")
            components = dict(getattr(result, "components", None) or {})
            return value, components
        except Exception:
            self.failures += 1
            if self.failures == 1:
                _LOG.warning(
                    "caliper grader %r raised; scoring %r (further failures are counted, "
                    "not logged)", self.name, self.on_error, exc_info=True,
                )
            return self.on_error, {}


def make_trl_reward_func(grader, name, on_error=0.0):
    """Build a TRL GRPOTrainer reward function around a caliper grader.

    Reads dataset columns `gold` and `caliper_metadata` (or `metadata`) from
    kwargs. The returned function's __name__ is `name` (TRL logs
    rewards/<name>/mean); `.scorer` exposes call/failure counters.
    """
    scorer = Scorer(grader, name, on_error)

    def reward_func(completions, **kwargs):
        n = len(completions)
        prompts = kwargs.get("prompts") or [""] * n
        golds = kwargs.get("gold") or [None] * n
        metas = kwargs.get("caliper_metadata")
        if metas is None:
            metas = kwargs.get("metadata")
        if metas is None:
            metas = [None] * n
        rewards = []
        for i in range(n):
            value, _ = scorer.score(
                messages_to_prompt(prompts[i]),
                completion_to_text(completions[i]),
                golds[i],
                decode_metadata(metas[i]),
            )
            rewards.append(value)
        return rewards

    reward_func.__name__ = name
    reward_func.__qualname__ = name
    reward_func.scorer = scorer
    return reward_func


def _gold_from_reference(ref):
    if ref is None or isinstance(ref, str):
        return ref
    return _json.dumps(ref, sort_keys=True)


def bedrock_score_batch(scorer, event):
    """Score a Bedrock RFT Lambda event; returns the documented output list."""
    if isinstance(event, list):
        items = event
    elif isinstance(event, dict) and isinstance(event.get("trajectories"), list):
        items = event["trajectories"]
    else:
        items = [event]

    out = []
    for item in items:
        messages = list(item.get("messages") or [])
        response = ""
        if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
            response = content_to_text(messages[-1].get("content"))
            messages = messages[:-1]
        meta = item.get("metadata") or {}
        caliper = item.get("caliper", meta.get("caliper"))
        if isinstance(caliper, dict):
            gold = caliper.get("gold")
            metadata = decode_metadata(caliper.get("metadata"))
        else:
            gold = _gold_from_reference(item.get("reference_answer", meta.get("reference_answer")))
            metadata = {k: v for k, v in meta.items() if k != "reference_answer"}
        value, components = scorer.score(messages_to_prompt(messages), response, gold, metadata)
        metrics = [{"name": scorer.name, "value": value, "type": "Reward"}]
        for key in sorted(components):
            metrics.append(
                {"name": scorer.name + "." + key, "value": components[key], "type": "Metric"}
            )
        out.append({"id": item.get("id"), "aggregate_reward_score": value, "metrics_list": metrics})
    return out


def openai_grade(scorer, sample, item):
    """OpenAI RFT python-grader body: grade(sample, item) -> float."""
    response = sample.get("output_text")
    if not isinstance(response, str):
        response = ""
    prompt = messages_to_prompt(item.get("messages") or item.get("prompt") or "")
    value, _ = scorer.score(
        prompt, response, item.get("gold"), decode_metadata(item.get("caliper_metadata"))
    )
    return value
