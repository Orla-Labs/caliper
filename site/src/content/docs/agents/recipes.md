---
title: "Recipes"
description: "Step-by-step Caliper tasks for agents. Each one states the goal, the steps, a command that verifies it worked, and what success looks like."
---

Each recipe ends with **Verify**: a command whose output tells you whether the step worked.
Recipes that call a real provider spend money; read [What costs money](/agents/spending/)
and get your user's go-ahead first.

## 1. Is it safe to switch to a cheaper model?

**Goal:** compare what production runs today (the reference) with one or more candidates,
on outcome and on cost.

1. Get a golden set: real requests as the feature sent them, one JSON object per line with
   at least `id` and `request` ([recipe 6](#6-build-a-golden-set-from-production-logs)
   builds one from logs). Put it next to the config as `golden.jsonl`.
2. Write `decide.py` ([recipe 4](#4-write-the-decide-function)).
3. Write `caliper.toml`. Replace the model ids and region with your own:

   ```toml
   golden = "golden.jsonl"
   decide = "decide.py:decide"
   reference = "current"      # what production runs today
   noise_replays = 2          # the reference runs twice; its own flips are the noise floor
   budget_usd = 5.0           # hard stop

   [[candidates]]
   name = "current"
   provider = "bedrock_converse"
   model = "<production model id>"
   region = "us-east-1"

   [[candidates]]
   name = "cheaper"
   provider = "bedrock_converse"
   model = "<candidate model id>"
   region = "us-east-1"

   [costs]                    # what each changed outcome costs; unlisted changes cost 1.0
   "approve->reject" = 1.0
   "reject->approve" = 5.0

   [ci]
   max_regression = 0.10      # allowed cost-weighted regression per row beyond noise

   [cost_projection]
   calls_per_day = 10000      # production volume, for $/month
   ```

4. Pilot on 20 rows to measure cost per row before spending on the whole set:

   ```bash
   head -n 20 golden.jsonl > golden.pilot.jsonl
   sed 's/^golden = .*/golden = "golden.pilot.jsonl"/; s/^budget_usd = .*/budget_usd = 1.0/' \
       caliper.toml > caliper.pilot.toml
   caliper eval --config caliper.pilot.toml --out build/pilot
   ```

5. Estimate the full run (see [What costs money](/agents/spending/#estimate-before-you-run)),
   confirm with your user, then run it with the gate:

   ```bash
   caliper eval --config caliper.toml --out build/eval-$(date +%Y%m%d-%H%M%S) --ci \
       --summary-md summary.md
   ```

**Verify:** the exit code is the verdict (`0` pass, `1` a candidate failed; reasons are
printed under the table). Then read the numbers:

```bash
jq '.candidates[] | {name, error_rate, cost_per_row, cost_vs_reference, monthly_delta_usd,
    excess: .paired_excess.delta}' build/eval-*/report.json
```

`excess.high` at or below `max_regression` means "no worse than the reference beyond its
own noise, at 95%". Check `.warnings` too: an approximate or missing price shows up there.

## 2. Gate pull requests in CI

**Goal:** a pull request that changes a prompt, model or `decide()` fails when the feature
regresses or gets too expensive, with a summary comment on the PR.

```yaml
# .github/workflows/caliper.yml
name: caliper
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install 'orlalabs-caliper[bedrock-converse]'   # the extras your providers need
      - name: Eval
        run: >
          caliper eval --config evals/caliper.toml --out build/eval --ci
          --summary-md build/eval/summary.md
        env:
          AWS_REGION: us-east-1          # plus credentials, e.g. via aws-actions/configure-aws-credentials
      - name: Post the summary
        if: always() && hashFiles('build/eval/summary.md') != ''
        run: |
          cat build/eval/summary.md >> "$GITHUB_STEP_SUMMARY"
          gh pr comment ${{ github.event.pull_request.number }} --body-file build/eval/summary.md
        env:
          GH_TOKEN: ${{ github.token }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: caliper-eval
          path: build/eval
```

- Keep `budget_usd` in the config so a runaway PR cannot spend without limit.
- Do not cache or restore `build/eval` between runs: a restored `test_ledger.jsonl` fails
  the gate with `test split re-touched`, and restored results are resumed.

**Verify:** open a PR; the `Eval` step's exit code is the gate, and the PR gets the
summary comment. Locally, the same command must exit `0` on the current `main`.

## 3. Fail when a candidate costs too much

**Goal:** the gate fails a candidate whose cost per row is above a multiple of the
reference's, and the report shows dollars per month.

```toml
[ci]
max_regression = 0.10
max_cost_ratio = 1.25        # at most 1.25x the reference's cost per row

[cost_projection]
calls_per_day = 20000        # feature calls a day (a vote loop's 3 replies are one call)
days_per_month = 30
```

A candidate (or reference) with replies priced at `$0` fails this rule: an unknown price
is never read as free. Fix it with [recipe 5](#5-price-a-model-caliper-cannot-price).

**Verify:**

```bash
jq '.candidates[] | {name, cost_vs_reference, monthly_cost_usd, monthly_delta_usd, unpriced_replies}' OUT/report.json
```

With `--ci`, a failing candidate prints
`cost 1.40x the reference > allowed 1.25x ($... vs $... per row; +$.../mo)`.

## 4. Write the decide function

**Goal:** `decide.py` reproduces exactly what the production feature does with the
model's replies, so Caliper compares decisions, not text.

```python
# decide.py: loaded by path from the config's folder; keep it self-contained.
from caliper_eval.types import Decision, Reply


def decide(replies: list[Reply]) -> Decision:
    """The feature's decision for one golden row, from its replies (one per replies_per_row)."""
    texts = [r.text for r in replies if r.error is None and r.text]
    if not texts:
        return Decision(outcome="fallback")          # what production does when the model fails
    answer = texts[0].strip().lower()
    return Decision(
        outcome="approve" if answer.startswith("yes") else "reject",
        score=None,                                  # a [0, 1] confidence enables calibration
        detail={"answer": answer[:80]},             # free-form, kept in results.jsonl
    )
```

- With `output_mode = "json_schema"` or `"tool"`, read `reply.parsed` instead of `text`.
- Map unusable replies to the outcome production would take. Raise only for real bugs: a
  raised exception leaves the row with no decision and counts against `max_error_rate`.
- Copy production's thresholds and tie-breaks exactly. If they live in production code,
  test that both agree (the bundled example does this in `tests/test_parity.py`).

**Verify** without spending anything:

```bash
python -c "
from caliper_eval.types import Reply
from decide import decide
print(decide([Reply(text='Yes, approve')]), decide([Reply(error='timeout')]))"
```

Expected: `outcome='approve'` for the first, your fallback outcome for the second. After
any later edit to `decide.py`, re-run evals with `--no-resume`.

## 5. Price a model Caliper cannot price

**Goal:** every candidate has a real price, so cost columns and `max_cost_ratio` work.

Caliper prices Bedrock models live from the AWS Price List, and Anthropic-API models from
a dated built-in table (with a warning). `openai_compat`, `fake` and any model it cannot
match get no price. Add one, in **USD per million tokens**:

```toml
[pricing."<model id exactly as in the candidate>"]
input = 0.80
output = 4.00
cache_read = 0.08            # optional, defaults to input
cache_write = 1.00           # optional, defaults to input
source = "https://provider.example/pricing, checked 2026-11-11"
```

`[pricing]` always wins over the live price.

**Verify:** `jq '[.candidates[] | {name, unpriced_replies}], .warnings' OUT/report.json`
shows `unpriced_replies: 0` and no `no price known` warning for that model.

## 6. Build a golden set from production logs

**Goal:** a golden set that keeps every rare outcome and samples the common one.

Input: JSONL logs, one line per feature call, with the request object (as the feature
sent it) and the outcome production recorded.

```bash
caliper sample --in logs.jsonl --out golden.jsonl \
    --stratum-field outcome --keep needs_review,fallback --negatives 200 \
    --id-field id --reference-field outcome
```

- `--keep` strata are kept whole; `--negatives` rows are sampled from the rest.
- Dotted paths reach nested fields (`--request-field call.request`).
- `--seed` makes the sample reproducible (default `0`).

**Verify:** the command prints `sampled N of M log lines` and a count per stratum; then
`caliper eval` loads the file without a `invalid golden row` error.

## 7. Label a sample and turn it into a golden set

**Goal:** human labels on golden rows, for calibration, matched operating points and
label-based slice rules. Needs `orlalabs-caliper[label]`.

```bash
caliper label init labels --data golden.jsonl --labels categories \
    --categories approve,reject,fallback --copy-data
caliper label labels            # opens the UI; a person labels (keys 1..9)
caliper label export labels --format golden --out golden.labeled.jsonl
```

The labelling itself is a human step: start the UI for your user and wait for them. Export
writes only rows with a resolved label (`--include-unlabelled` keeps the rest unchanged).
Point `golden` in `caliper.toml` at `golden.labeled.jsonl`.

**Verify:** `jq -c 'select(.label != null) | .label' golden.labeled.jsonl | sort | uniq -c`
counts the labelled rows per outcome.

## 8. Resume after a budget stop or provider errors

- **Exit `4`** (budget reached): results so far are in `OUT/results.jsonl`. Re-run the
  **same command**; it skips finished rows. Raise `--budget-usd` only with your user's
  agreement.
- **Provider errors** (rate limits, timeouts): `--retry-errors` re-runs only the rows whose
  call failed.
- **Changed config or golden rows:** resume re-runs rows whose request or candidate
  settings changed. Changed `decide.py` or fake scripts are not detected: use
  `--no-resume`.

**Verify:** the re-run reports the full row count per candidate (`rows` in `report.json`
equals `n_golden`) and `stale_results` is `0`.

## 9. Check a reward grader against humans, then compile it

**Goal:** a grader for RL fine-tuning that agrees with human labels, packaged for TRL or
Bedrock RFT. Try it on the bundled example:

```bash
caliper example invoice_totals inv
caliper kappa --rollouts inv/labeled_rollouts.jsonl --grader inv/graders.py:fixed_grader
caliper-compile --target trl --grader inv/graders.py:fixed_grader \
    --dataset inv/prompts.jsonl --out inv/trl
```

**Verify:** `caliper kappa` prints `kappa +0.868` for this example (add `--json` to parse
it); `caliper-compile` prints `wrote inv/trl/train.jsonl` and `wrote inv/trl/reward_funcs.py`
and exits `0`. The concepts are in [Compile targets](/graders/compile-targets/).
