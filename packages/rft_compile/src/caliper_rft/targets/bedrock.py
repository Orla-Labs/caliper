"""Amazon Bedrock reinforcement fine-tuning (RFT) target.

Emits ``train.jsonl`` (OpenAI chat-completion format, as Bedrock RFT requires)
and a self-contained ``lambda_function.py`` (handler ``lambda_function.lambda_handler``)
to use as the job's Lambda reward function ("Custom code (RLVR)").

Official AWS docs this is written against (checked 2026-11):
    https://docs.aws.amazon.com/bedrock/latest/userguide/reinforcement-fine-tuning.html
        supported models: amazon.nova-2-lite-v1:0:256k (us-east-1),
        openai.gpt-oss-20b and qwen.qwen3-32b (us-west-2).
    https://docs.aws.amazon.com/bedrock/latest/userguide/rft-prepare-data.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/rft-prepare-data-open-weight.html
        rows: {"messages": [...], "reference_answer": <any>, <optional extra fields>};
        extra fields "will be passed to your reward function in the metadata field";
        100-20K records.
    https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions-open-weight.html
        Lambda input: list of {"id", "messages" (ending with the assistant turn),
        "metadata": {"reference_answer", ...extra}}; output: list of {"id",
        "aggregate_reward_score", "metrics_list": [{"name", "value", "type"}]}.
    https://docs.aws.amazon.com/bedrock/latest/userguide/rft-submit-job.html
        CreateModelCustomizationJob customizationType=REINFORCEMENT_FINE_TUNING,
        customizationConfig.rftConfig.graderConfig.lambdaGrader.lambdaArn.
    https://docs.aws.amazon.com/bedrock/latest/userguide/rft-access-security.html
        service role needs lambda:InvokeFunction on the reward function.

Row layout we emit::

    {"id": ..., "messages": [{"role": "user", "content": <prompt>}],
     "reference_answer": <gold or "">,
     "caliper": {"gold": <gold|null>, "metadata": {...}}}

``caliper`` is an extra field, so Bedrock forwards it in the Lambda event's
``metadata``; it lets the Lambda rebuild the exact Rollout (gold may be null,
metadata keys can't collide with Bedrock's own fields). ``reference_answer``
stays populated for Bedrock's own validation/console. The Lambda also accepts
rows not produced by caliper (falls back to ``reference_answer`` + metadata).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from caliper_sdk import EvalRow

from caliper_rft.targets._common import GraderSource, render_artifact, write_jsonl, write_text

TRAIN_FILE = "train.jsonl"
LAMBDA_FILE = "lambda_function.py"


def bedrock_record(row: EvalRow) -> dict[str, Any]:
    r = row.rollout
    return {
        "id": row.id,
        "messages": [{"role": "user", "content": r.prompt}],
        "reference_answer": r.gold if r.gold is not None else "",
        "caliper": {"gold": r.gold, "metadata": r.metadata},
    }


_TAIL = '''\
# ---------------------------------------------------------------------------
# AWS Lambda entry point (handler: lambda_function.lambda_handler)
# Docs: https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions.html
# Grader failures score ON_ERROR (Bedrock needs a number); counts are logged.
# ---------------------------------------------------------------------------
_SCORER = Scorer(GRADER, GRADER_NAME, 0.0 if ON_ERROR is None else ON_ERROR)


def lambda_handler(event, context):
    failures_before = _SCORER.failures
    results = bedrock_score_batch(_SCORER, event)
    failed = _SCORER.failures - failures_before
    if failed:
        _LOG.warning("caliper grader %r failed on %d/%d samples", GRADER_NAME, failed, len(results))
    return results


__all__ = ["lambda_handler"]
'''


def render_lambda(grader: GraderSource, *, on_error: float | None = 0.0) -> str:
    return render_artifact(
        target="bedrock",
        grader=grader,
        on_error=on_error,
        tail=_TAIL,
        docstring=(
            f"Amazon Bedrock RFT Lambda reward function for caliper grader {grader.name!r}.\n\n"
            "Self-contained (stdlib only). Deploy with handler `lambda_function.lambda_handler`\n"
            "(Python 3.12 runtime recommended) and reference its ARN in\n"
            "rftConfig.graderConfig.lambdaGrader.lambdaArn."
        ),
    )


def export_bedrock(
    grader: GraderSource,
    rows: list[EvalRow],
    out_dir: str | Path,
    *,
    on_error: float | None = 0.0,
) -> list[str]:
    """Write ``train.jsonl`` + ``lambda_function.py`` into ``out_dir``."""
    out = Path(out_dir)
    write_jsonl(out / TRAIN_FILE, [bedrock_record(r) for r in rows])
    write_text(out / LAMBDA_FILE, render_lambda(grader, on_error=on_error))
    return [TRAIN_FILE, LAMBDA_FILE]
