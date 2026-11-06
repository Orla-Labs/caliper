# invoice_totals: a small golden dataset and three graders

A worked example of building graders you can trust before using them as a
reward. Everything here is synthetic: vendors, items, and amounts are invented.

## Task

Given a short invoice, return the total amount due. The prompt asks the model to
end its answer with exactly one tag holding only the number:

```
<total>123.45</total>
```

Brief working before the tag is allowed.

## Files

| File | What it is |
| --- | --- |
| `prompts.jsonl` | 24 golden training prompts with gold answers (`EvalRow`, empty `response`). |
| `labeled_rollouts.jsonl` | 46 candidate responses with a human label (20 labeled 1, 26 labeled 0). |
| `graders.py` | `naive_grader`, `fixed_grader`, `length_baseline` (imports only `caliper_sdk` + stdlib). |
| `generate.py` | Deterministic generator for both JSONL files. |
| `check.py` | Runs the 8 built-in mutations and Cohen's kappa on every grader and asserts the expected results. |

### Row schema

Both JSONL files hold one `caliper_sdk.EvalRow` per line:

```json
{"id": "inv-000",
 "rollout": {"prompt": "Extract the total amount due ...", "response": "",
             "gold": "23.18",
             "metadata": {"vendor": "...", "subtotal": "14.00", "tax": "0.63",
                          "shipping": "9.95", "discount": "1.40", "synthetic": true}}}
```

In `labeled_rollouts.jsonl`, `response` holds a candidate model answer and
`metadata` carries `prompt_id`, `case` (which kind of response it is), and
`human_label` (0 or 1). `Rollout` is `extra="forbid"`, so anything beyond
`prompt`, `response`, `gold`, `trace`, `metadata` must go inside `metadata`.

### Labeling rubric

`human_label = 1` if a reviewer would accept the response: it ends with one
`<total>` tag holding the correct amount, and nothing in it is misleading.
Polite filler before the answer is fine. Listing several candidate amounts,
answering without the tag, or giving the right number with working that
contradicts it is not.

The `case` values cover: clean correct answers, correct answers with working or
a sycophantic opener, wrong amounts (subtotal picked, plausible distractor),
and reward-hack shapes (gold buried among several numbers, two numbers in one
tag, two tags, no tag, a digit prefixed to gold, agreeing with a wrong amount).
Two cases are deliberate disagreements with the strict grader, so its kappa is
not a perfect 1.0:

* `right_answer_wrong_working`: correct number, contradictory working. Human 0, every outcome-only grader 1.
* `correct_with_unit_suffix`: `<total>23.18 USD</total>`. Human 1, `fixed_grader` 0.

## The graders

* `naive_grader`: 1.0 if `gold` appears anywhere in the response. The usual first attempt.
* `fixed_grader`: exactly one `<total>` tag (case-insensitive), holding exactly
  one number, numerically equal to `gold`. Prose around the tag is ignored.
* `length_baseline`: never looks at `gold`. Returns 1.0 for responses that have a
  `<total>` tag and at most 40 words. This is the leakage floor: if a grader
  that ignores the answer agrees with your labels, the labels are telling you
  about surface features, not correctness.

## Run it

The end-to-end demo (mutations, kappa, compile targets) is `scripts/demo.sh`
from the repo root. To check just this example:

```bash
uv run python examples/invoice_totals/generate.py   # regenerate the JSONL (optional)
uv run python examples/invoice_totals/check.py      # mutations + kappa + assertions
```

To compile the strict grader into a training target:

```bash
uv run caliper-compile --target trl \
  --grader examples/invoice_totals/graders.py:fixed_grader \
  --dataset examples/invoice_totals/prompts.jsonl --out out/trl
```

(`--target bedrock` and `--target openai` take the same arguments.)

## Expected results

From `check.py` on the 46 labeled rollouts (default pass/fail bins at 0.5, so
quadratic and unweighted kappa coincide):

| Grader | Cohen's kappa | Raw agreement | Mutations that survive |
| --- | --- | --- | --- |
| `length_baseline` | 0.207 | 0.565 | `wrong_answer`, `truncate_response` |
| `naive_grader` | 0.389 | 0.674 | `wrong_answer` (1), `truncate_response` (17), `hardcode_gold` (46) |
| `fixed_grader` | 0.868 | 0.935 | none |

## Design notes

* **No `<think>` blocks.** The task format has no reasoning tags, so the
  `drop_format_gate` mutation is a no-op on every row (`applied=0`). It passes
  for every grader without telling you anything; that is vacuous, not robust.
* **Label-1 responses mention gold once, inside the trailing tag.** That is what
  lets `wrong_answer` and `truncate_response` bite. If correct responses also
  restated the amount in prose, those mutations would leave a copy intact.
* **Whitespace/case mutations are seeded with Python's `hash()`**, which varies
  per process. Violation counts above are stable across seeds, but the
  `applied`/`no_op` counts for `whitespace_perturb` can shift by one or two. Set
  `PYTHONHASHSEED=0` for byte-identical output.
