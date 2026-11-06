# Compile targets

`caliper-compile` turns a Caliper `@grader` and an EvalRow dataset into the files a training stack consumes. The same grader you debugged in the editor becomes the reward function at train time.

```bash
uv run caliper-compile --target {trl,bedrock,openai} \
    --grader path/to/graders.py:ATTR \
    --dataset path/to/rows.jsonl \
    --out DIR \
    [--on-error SCORE|none]
```

| target | status | files written | runs the grader in |
|---|---|---|---|
| `trl` | supported | `train.jsonl`, `reward_funcs.py` | your GRPOTrainer process (Hugging Face TRL) |
| `bedrock` | supported | `train.jsonl`, `lambda_function.py` | an AWS Lambda called by Amazon Bedrock reinforcement fine-tuning |
| `openai` | **legacy / deprecated** | `rft_job.json`, `train.jsonl` | OpenAI's hosted python grader |

## Inputs

- **`--grader PATH.py:ATTR`** names a Python file and the `@grader`-decorated callable in it. `:ATTR` may be omitted when the file defines exactly one grader. The file can `from caliper_sdk import grader, Rollout, GraderResult` (or `from caliper_sdk.types import ...`) and import any module that exists where training runs. The stdlib is always safe.
- **`--dataset`** is EvalRow JSONL: `{"id": str, "rollout": {"prompt": str, "response": str, "gold": str|null, "metadata": {...}}}`. `response` is ignored on export and may be `""`. `prompt`, `gold` and `metadata` are carried through. Rows keep their input order.
- **`--on-error`** sets the reward used when the grader raises or returns an invalid score. The default is `0.0`. `none` is trl-only and makes TRL skip the sample for this reward function. The first failure is logged with its traceback on the `caliper.reward` logger, and every failure is counted.

## Self-contained artifacts

Every emitted `.py` file contains, in this order:

1. A small stdlib-only copy of `grader`, `Rollout` and `GraderResult` that mirrors `caliper_sdk` semantics: score in [0, 1], bools rejected, dict coerced to Rollout.
2. The adapter runtime.
3. Your grader file's source, verbatim.

`caliper_sdk` imports inside your grader resolve to that copy. **Caliper does not need to be installed where training runs**, and neither does pydantic. Python >= 3.10 is required.

Tests and `scripts/smoke_compile_targets.py` check this by running each artifact under `python -I -S`, with no site-packages. A parity test also checks that each emitted file returns exactly the same rewards as the in-process library API running the real SDK.

Output is deterministic: JSON keys are sorted, files contain no timestamps, and they contain no absolute paths (only the grader file's basename and the sha256 of its source). Re-running produces byte-identical files.

## `trl`: Hugging Face TRL GRPOTrainer

References:
- https://huggingface.co/docs/trl/main/en/grpo_trainer ("Using a custom reward function")
- https://huggingface.co/docs/trl/main/en/dataset_formats

**`train.jsonl`** has one row per EvalRow, in the **conversational** format:

```json
{"caliper_metadata": "{\"currency\": \"USD\"}", "gold": "20", "id": "inv-1", "prompt": [{"content": "2 x $10. Total?", "role": "user"}]}
```

Conversational format makes TRL apply the policy model's chat template. Instruct and chat open-weight models (Qwen3, gpt-oss, Llama-Instruct) expect that template, and Bedrock uses the same `messages` shape.

TRL passes every column except `prompt` to reward functions as a keyword argument:
- `gold` holds `Rollout.gold`.
- `caliper_metadata` holds `Rollout.metadata` as a JSON string. A string keeps arbitrary per-row dicts exact; Arrow would otherwise merge differing keys into one struct and fill the gaps with null.

Keep `remove_unused_columns=False`, which is the GRPOConfig default.

**`reward_funcs.py`** exposes `REWARD_FUNC` and `reward_funcs = [REWARD_FUNC]`. The function follows TRL's contract, `f(completions, **kwargs) -> list[float]`:
- It accepts string completions (standard format) and message-list completions (conversational format).
- It rebuilds a `Rollout(prompt, response, gold, metadata)` for each completion.
- It returns one float per completion.
- Its `__name__` is the grader's name, so TRL logs `rewards/<grader name>/mean`.

```python
from datasets import load_dataset
from trl import GRPOConfig, GRPOTrainer

from reward_funcs import reward_funcs  # DIR/reward_funcs.py

train = load_dataset("json", data_files="DIR/train.jsonl", split="train")
trainer = GRPOTrainer(
    model="Qwen/Qwen3-0.6B",
    reward_funcs=reward_funcs,          # mix with other reward funcs if you like
    args=GRPOConfig(output_dir="grpo-out"),
    train_dataset=train,
)
trainer.train()
```

You can also use the library API in-process when caliper is installed in the training environment:

```python
from caliper_rft import to_trl_reward_func
from my_graders import fixed_grader

reward = to_trl_reward_func(fixed_grader)          # __name__ == "fixed_grader"
reward(completions=["Total: $20"], prompts=["..."], gold=["20"])  # -> [1.0]
```

Caliper itself never imports `trl`, `torch` or `datasets`.

## `bedrock`: Amazon Bedrock reinforcement fine-tuning

References:
- https://docs.aws.amazon.com/bedrock/latest/userguide/reinforcement-fine-tuning.html (supported models)
- https://docs.aws.amazon.com/bedrock/latest/userguide/rft-prepare-data.html and .../rft-prepare-data-open-weight.html (dataset)
- https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions.html and .../reward-functions-open-weight.html (Lambda contract)
- https://docs.aws.amazon.com/bedrock/latest/userguide/rft-submit-job.html (job API)
- https://docs.aws.amazon.com/bedrock/latest/userguide/rft-access-security.html (IAM)

Supported base models, as documented in 2026-11:

| model | model ID | region |
|---|---|---|
| Amazon Nova 2 Lite | `amazon.nova-2-lite-v1:0:256k` | us-east-1 |
| gpt-oss-20B | `openai.gpt-oss-20b` | us-west-2 |
| Qwen3 32B | `qwen.qwen3-32b` | us-west-2 |

Bedrock requires 100 to 20K training records. The CLI warns on stderr below 100 but still writes the files.

**`train.jsonl`** uses the OpenAI chat-completion format with `reference_answer`, plus one extra field, `caliper`:

```json
{"caliper": {"gold": "20", "metadata": {"currency": "USD"}}, "id": "inv-1", "messages": [{"content": "2 x $10. Total?", "role": "user"}], "reference_answer": "20"}
```

Bedrock forwards extra fields to the reward Lambda in the event's `metadata`. `caliper` lets the Lambda rebuild the exact Rollout: `gold` may be null, and metadata keys can't collide with Bedrock's own fields. `reference_answer` is the gold value, or `""` when gold is null.

**`lambda_function.py`** (handler `lambda_function.lambda_handler`) receives the documented batch, one entry per rollout. The rollout's last message is the model's assistant turn:

```json
[{"id": "inv-1", "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "Total: $20"}],
  "metadata": {"reference_answer": "20", "caliper": {"gold": "20", "metadata": {}}}}]
```

It returns the documented response:

```json
[{"id": "inv-1", "aggregate_reward_score": 1.0,
  "metrics_list": [{"name": "total_matches", "value": 1.0, "type": "Reward"},
                   {"name": "total_matches.correct", "value": 1.0, "type": "Metric"}]}]
```

The handler adds the grader's `GraderResult.components` as `Metric` entries. It also accepts `{"trajectories": [...]}`, which is the shape in AWS's sample handler, as well as a single item. It finds `reference_answer` / `caliper` either at the top level or under `metadata`. For rows not produced by caliper, `reference_answer` becomes `gold` (JSON-encoded if it's an object) and the remaining metadata becomes `Rollout.metadata`.

To deploy:
1. Zip `lambda_function.py` and create a Python 3.12 Lambda with handler `lambda_function.lambda_handler`. Raise the timeout well above the 3 s default; AWS allows up to 15 min.
2. Give the Bedrock customization service role `lambda:InvokeFunction` on the function.
3. Upload `train.jsonl` to S3 and submit the job:

```python
import boto3

bedrock = boto3.client("bedrock", region_name="us-east-1")
bedrock.create_model_customization_job(
    jobName="caliper-rft-1",
    customModelName="invoice-totals-rft",
    customizationType="REINFORCEMENT_FINE_TUNING",
    roleArn="arn:aws:iam::123456789012:role/BedrockRftRole",
    baseModelIdentifier="arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0:256k",
    trainingDataConfig={"s3Uri": "s3://my-bucket/rft/train.jsonl"},
    outputDataConfig={"s3Uri": "s3://my-bucket/rft/output/"},
    customizationConfig={"rftConfig": {
        "graderConfig": {"lambdaGrader": {
            "lambdaArn": "arn:aws:lambda:us-east-1:123456789012:function:caliper-reward"}},
        "hyperParameters": {"epochCount": 2, "batchSize": 64, "learningRate": 0.00001,
                            "trainingSamplePerPrompt": 4},
    }},
)
```

For gpt-oss and Qwen3, which go through Bedrock's OpenAI-compatible fine-tuning API at https://docs.aws.amazon.com/bedrock/latest/userguide/fine-tuning-openai-job-create.html, use the same Lambda ARN in `method.reinforcement.grader = {"type": "lambda", "lambda": {"function": "<arn>"}}`.

**Not confirmed in AWS docs.** The handler is built to tolerate these unknowns:
- the exact event wrapper (a bare list in the docs, `{"trajectories": ...}` in the AWS blog sample);
- whether `reference_answer` arrives at the top level or under `metadata`;
- the batch size per invocation;
- whether a score range is required (we emit [0, 1]);
- how errors and retries are handled (we never raise for grader errors; they score `ON_ERROR`);
- whether `metrics_list` is optional and what `Reward` vs `Metric` mean beyond the docs' example;
- whether `reference_answer: ""` and a nested-object extra field (`caliper`) pass Bedrock's dataset validation.

## `openai`: legacy / deprecated

> **Deprecated.** OpenAI is winding down its self-serve fine-tuning platform. Since 2026-05-07 it has been closed to organizations that had not run fine-tuning before. Active existing customers can no longer create fine-tuning jobs after **2027-01-06**. The `o4-mini-2025-04-16` RFT base model shut down on 2026-10-23. Sources:
> - https://developers.openai.com/api/docs/deprecations ("Update to OpenAI's self-serve fine-tuning")
> - https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning ("OpenAI is winding down the fine-tuning platform.")
>
> The target stays only for existing customers. The CLI prints a deprecation warning on stderr. Use `trl` or `bedrock` instead.

The target writes two files:
- **`rft_job.json`** is the `POST /v1/fine_tuning/jobs` body from `caliper_rft.build_payload`. `model` and `training_file` hold the placeholders `<BASE_MODEL>` and `<UPLOADED_TRAINING_FILE_ID>`, which you replace. `method.reinforcement.grader` is a `python` grader whose `source` contains the shim, your grader and `grade(sample, item)`. That function reads `sample["output_text"]` and the training row's `messages`, `gold` and `caliper_metadata` (graders guide: https://developers.openai.com/api/docs/guides/graders).
- **`train.jsonl`** has rows of the form `{"messages": [...], "gold": ..., "caliper_metadata": "..."}`.

## Library API

```python
from caliper_rft.targets import GraderSource, compile_target, load_dataset, to_trl_reward_func

g = GraderSource.from_spec("examples/invoice_totals/graders.py:fixed_grader")
rows = load_dataset("examples/invoice_totals/prompts.jsonl")
compile_target("trl", g, rows, "build/trl")   # -> ["train.jsonl", "reward_funcs.py"]
```

Smoke test, offline with no keys: `uv run python scripts/smoke_compile_targets.py`.
