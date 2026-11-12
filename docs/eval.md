# `caliper eval`

`caliper eval` answers one question for an LLM feature: *if we ship this model or
prompt change, what will the feature do differently, and is that worse than
the noise we already live with?* It replays a golden set of real requests
through each candidate, applies your feature's own decision logic, and compares
the outcomes to a reference run, with costs and a CI gate attached.

The worked example is [`examples/catalog_cleanup`](../examples/catalog_cleanup/README.md).
`caliper example catalog_cleanup` copies it to `./catalog_cleanup`, and
`caliper eval --config catalog_cleanup/caliper.toml` runs it offline in about a second.

- [Concepts](#concepts)
- [Golden set format](#golden-set-format)
- [Config reference](#config-reference)
- [CLI reference](#cli-reference)
- [Output files](#output-files)

## Concepts

### Grade the system outcome, not single replies

Real features wrap the model in logic: vote loops, confidence thresholds,
retries, fallbacks. A model change that alters one reply in three may change
nothing the user sees, and a change that looks small per reply may flip what
the feature does. Caliper therefore grades the feature's **outcome**:

```python
from caliper_eval.types import Decision, Reply

def decide(replies: list[Reply]) -> Decision:
    ...  # your feature's logic, applied to this row's replies
    return Decision(outcome="auto_publish", score=0.91, detail={"tax_code": "CTX-510"})
```

- `outcome` is a categorical string. Transitions are computed on it.
- `score` (optional, in [0, 1]) is the confidence used for calibration.
- `detail` is free-form and is stored in the results.

`decide` may also return a plain string (the outcome) or a dict. It is called
even when replies carry an `error`, because how the feature handles a failed
call is part of what you are testing. If `decide` raises, the row is recorded
with an error and no decision. `replies_per_row` on the candidate sets how many
replies `decide` receives, for example 3 for a vote loop.

### Parity: prove the mirror matches production

`decide` is a mirror of logic that lives somewhere in your service, so it can
drift. `caliper_eval.decision.assert_parity` enumerates **every** reply sequence
built from an alphabet of representative replies and checks that the mirror and
the real rule agree:

```python
from caliper_eval.decision import assert_parity

assert_parity(
    decide,                      # the mirror Caliper replays
    production_rule,             # the real function, any shape
    alphabet,                    # Reply objects or plain strings (= Reply(text=s))
    length=[0, 1, 2, 3],         # sequence lengths to enumerate
    adapter=lambda rs: [r.text for r in rs],   # convert replies to what the real rule takes
)
```

If the real rule returns something other than an outcome string (an Enum, a
bool, a dict), pass `outcome_of=` to map it before the comparison. It raises
`ParityError` on the first counterexample, with the sequence and both
outcomes, and returns the number of sequences checked. Exceptions count as a
mismatch unless both sides raise the same type. Put the edges in the alphabet:
a score exactly at the threshold, `true` or `NaN` confidences, truncated JSON,
error replies, and structured (parsed-only) replies. The example's
[`tests/test_parity.py`](../examples/catalog_cleanup/tests/test_parity.py) folds
the code and the score into the compared value, so a mirror that routes
correctly but reports the wrong code still fails.

### Noise floor first

LLMs are not deterministic, even at the same settings. Before blaming a
candidate for a changed outcome, Caliper measures how often the **reference
itself** changes outcome when replayed. The reference runs `noise_replays`
times (default 2). Per row, replicas 1 and up are compared with replica 0:

- **flip rate**: share of rows whose outcome differs between replays.
- **cost-weighted flip rate**: the mean transition cost of those flips,
  symmetrized as `(cost(a->b) + cost(b->a)) / 2`, because replay order is arbitrary.
- **95% bootstrap CI**: rows resampled with replacement (`--n-boot`, default 1000),
  with a seed derived from `seed`.

The gate does not compare the candidate's interval with this one. It pairs
them row by row: on each row, the reference's own replay cost is subtracted
from the candidate's cost on that same row, and the bootstrap runs on those
differences (see [CI gate](#ci-gate)).

### Transitions and costs

Each candidate's outcome on replica 0 is compared with the reference's replica
0, row by row. That gives a transition matrix (`auto_publish -> needs_review: 19`)
and the list of rows that flipped. Not every change is equally bad, so
`[costs]` weights them:

```toml
[costs]
"auto_publish->needs_review" = 1.0   # a person has to check a record
"needs_review->auto_publish" = 5.0   # a record goes live without the check
```

Keys are exact `"from->to"` outcome strings. An unchanged outcome costs 0. A
change not listed costs **1.0**. The **regression** of a candidate is the sum
of transition costs divided by the rows compared. It can exceed 1 when costs
exceed 1, and it gets a bootstrap CI like the noise floor. The report's
**excess vs noise (paired)** column is the paired bootstrap CI of
`regression - noise` per row, which is what the gate reads.

Transitions are only as fine-grained as your outcome. If the outcome is
`auto_publish` and the detail that changed (say, the tax code) lives in
`detail`, the matrix will not show it. Put it in the outcome string when you
need it.

### Candidates: model + thinking + output mode

A candidate is the whole request shape, not just a model name:

| field | meaning |
| --- | --- |
| `provider` | `fake`, `anthropic` (direct), `bedrock` (Anthropic models on Amazon Bedrock, Messages API), `bedrock_converse` (any Bedrock model through the Converse API: Nova, Llama, Claude, ...), `openai_compat` (any chat-completions endpoint) |
| `model` | model ID as the provider names it |
| `thinking` | `off` (default) or `on` |
| `output_mode` | `text` (default), `json_schema` (provider-native structured output against `output_schema`), `tool` (one forced tool call whose input is the output) |
| `replies_per_row` | replies per row handed to `decide` |
| `params` | extra request parameters (`effort`, `max_tokens`, vendor knobs) |

The golden request is sent as stored. The candidate overlays its model, thinking
and output settings, and nothing else is rewritten. On Anthropic:

- `thinking = "off"` sends `{"type": "disabled"}`. Claude Opus 5.5 and Claude
  Sonnet 5.5 reject that with a 400, so use `thinking = "on"` (adaptive) and
  `params = { effort = "low" }` on those models.
- `thinking = "on"` sends `{"type": "adaptive"}`.
- `thinking_budget_tokens` sends the older `{"type": "enabled", "budget_tokens": N}`,
  which only older models accept.
- `params.effort` maps to `output_config.effort`.
- `json_schema` maps to `output_config.format`.
- `tool` forces `tool_choice`, which the newest models reject. Use `json_schema` there.
- Thinking tokens count against `max_tokens`. Raise it through `params` for
  thinking candidates.

On `openai_compat`, `thinking = "on"` is rejected locally. Put the vendor's own
knob (`reasoning_effort`, ...) in `params`. The API key comes from the
environment variable named by `params.api_key_env` (default `OPENAI_API_KEY`).
No server-side fallback is ever enabled.

On `bedrock_converse` (install `orlalabs-caliper[bedrock-converse]`; AWS credentials
from the usual boto3 chain):

- `model` is a base model ID or an inference profile ID
  (`global.amazon.nova-2-lite-v1:0`). The prefix also picks the price tier.
- `max_tokens`, `temperature`, `top_p` and `stop_sequences` (from the golden
  request or `params`) go to `inferenceConfig`. Other `params` keys must be
  Converse body fields (`additionalModelRequestFields`, `inferenceConfig`,
  `outputConfig`, `serviceTier`, ...) and are merged in as given. Anything else
  is an error.
- Thinking has no portable Converse switch, so `thinking = "on"` is rejected
  locally. Put the model's own field under `additionalModelRequestFields`.
  Nova 2 Lite with reasoning:
  `params = { additionalModelRequestFields = { reasoningConfig = { type = "enabled", maxReasoningEffort = "low" } } }`.
  Reasoning tokens are billed as output tokens.
- `json_schema` maps to `outputConfig.textFormat`; `tool` to a forced
  `toolConfig.toolChoice`.
- The Converse response does not say which model served it, so "who answered"
  shows `unknown` for these candidates and preflight cannot catch a
  substitution. It still catches a rejected shape.

### Preflight and who answered

Before touching the golden set, `caliper eval` sends one minimal request per
candidate, with the candidate's exact shape (model, thinking, output mode,
params). The fake provider sends the first golden row instead. The shape counts
as accepted when a response comes back from the requested model, even if that
tiny answer was truncated. A rejected shape (for example a 400), no response,
or an answer from a different model fails in seconds with the provider's
reason, and the run exits 3 before anything else is spent. Fake scripts can
reject shapes offline with `supports(candidate) -> str | None`. Skip the
preflight with `--skip-preflight`.

Every reply records **who actually served it**: provider, the model the response
reports, the endpoint, and whether that matches the request. Fallbacks,
substitutions and region prefixes are surfaced, never silently accepted. The
report's "who answered" column lists the served models, and any mismatch fails
the CI gate.

### Budget and resume

- `budget_usd` (or `--budget-usd`) is a hard cap. Before each row, Caliper
  estimates its cost from the candidate's mean cost so far. It stops with exit 4
  rather than exceed the cap, and prints a resume hint. The estimate can overshoot by one row on
  a candidate's first row.
- Results are appended to `OUT/results.jsonl`, one line per
  (candidate, row, replica). A re-run skips what is already there, so an
  interrupted or budget-stopped run picks up where it stopped. The report is
  byte-identical to an uninterrupted run. Use `--no-resume` to start fresh and
  `--retry-errors` to re-run rows whose provider call failed.
- Resume is stale-aware. Each result stores the hash of its golden request and
  of its candidate settings, and a row whose golden request or candidate changed
  is re-run. An edited `decide`, or edited code in a fake script, is **not**
  detected, so pass `--no-resume` after changing either. Results that still look stale are reported as a warning and fail the gate.
- Run IDs are derived from the candidates and the golden set, so budget, cost
  and CI edits keep the same run ID. There are no timestamps
  anywhere, so fake-provider output is byte-identical across runs and machines.

### Where prices come from

Every reply is priced in USD per million tokens, with input, output, cache read
and cache write priced separately. Caliper takes the price from the provider
whenever the provider publishes one, in this order:

1. `[pricing."<model>"]` in the config always wins.
2. **Amazon Bedrock** (`bedrock`, `bedrock_converse`): the live
   [AWS Price List](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html)
   offer files (`AmazonBedrock` and `AmazonBedrockFoundationModels`), which
   are public and need no credentials. The price is for the candidate's
   `region` (or `AWS_REGION`) and the tier its model ID implies: `global.`
   gets the global price, `us.`/`eu.`/... and bare IDs the regional one
   (+10%). Batch, flex, priority and 1-hour cache-write rates are not used.
   The price list names models by display name, which Caliper reads from
   Bedrock's `GetFoundationModel` (so it needs `bedrock:GetFoundationModel`).
   A model it cannot match exactly gets no price, never a nearest match.
   Files are cached for 24 hours under `~/.cache/caliper` (or
   `$CALIPER_CACHE_DIR`); `CALIPER_PRICING_OFFLINE=1` reads only the cache.
   Models with higher long-context rates (Claude Haiku 5.5 above its prompt
   threshold) get a warning: replies with very large prompts cost more than
   reported.
3. **Anthropic API direct** (`anthropic`): Anthropic publishes no pricing API
   (the Models API returns capabilities, not prices). These are priced from a
   built-in table copied from the
   [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) on
   2026-11-06, and every report that uses it says so in a warning. A Bedrock
   Claude candidate whose live lookup fails falls back to the same table, with
   a warning.
4. **Anything else** (`openai_compat`, `fake`): no price source. Add
   `[pricing]`. For OpenAI-compatible endpoints, cached prompt tokens are
   subtracted from input and billed as cache reads.

An unknown price costs $0 in the dollar column and is flagged in the report,
the `$/row` column shows `unpriced`, and under `[ci] max_cost_ratio` it fails
the gate.

### Cost per row, the cost gate, and the monthly projection

The report's **`$/row (x ref)`** column is each candidate's dollars divided by
its (row, replica) executions, next to its ratio to the reference. The
reference is replayed `noise_replays` times, so its raw dollar total is
inflated by those replays; cost per row divides them out. A vote loop's extra
replies stay in, because production pays for them.

- `[ci] max_cost_ratio = 1.25` fails any candidate whose cost per row is above
  1.25x the reference's. A candidate or reference with replies that used
  tokens but cost $0 fails too, with "cost unknown".
- `[cost_projection] calls_per_day = 20000` (and optional
  `days_per_month = 30`) adds a line with each candidate's projected monthly
  cost and its difference from the reference, in the terminal output and the
  PR summary. `calls_per_day` counts feature calls (golden-row equivalents), not
  replies.
- A model with no known price costs $0 and is flagged in the report.

Dollars and tokens include every replica, so the reference's noise replays are
counted.

### Better, not just different: quality against human approvals

Every measure above treats the reference as right. A candidate that changes an
outcome is charged for it, so those measures can say "the same" or "worse", never
"better". To pay more for a better model you need evidence of better: a person's
verdict on the outcomes themselves.

**Approvals are verdicts on outcomes.** An approval says whether a person would
accept one outcome for one golden row (`row_id`, `outcome`, `approved`). It names
no model, so it serves every candidate that reached that outcome on that row,
including candidates added later. It goes stale only when the row's request
changes (`request_sha256`). An outcome a person rejected is an **error**, and so
is no decision at all.

Only human verdicts count (`internal_reviewer`, `human_end_user`, `adjudicated`).
`approved: null` (Unsure) is no verdict. For one row and outcome, an adjudicated
approval wins over other approvals, and any approval wins over the row's golden
label. Approvals that contradict each other are no verdict, and are reported.

```toml
[quality]
approvals = ["review-1/approvals.jsonl"]   # one or more files, relative to the config
golden_labels = true                       # default: a human golden `label` approves that
                                           # outcome and rejects every other one
base_sample = 30                           # default: rows drawn at random to measure the
                                           # reference's error rate
```

A golden label that is not an outcome any candidate decided (a `yes` from a yes/no
project, say) judges nothing and is reported. So is a real outcome that no
candidate in this run happened to decide; that row then needs a review. An
adjudicated golden label can be overridden only by an adjudicated approval.

**Two numbers, two sources of evidence.**

1. **Improvement**, per candidate. When the reference and a candidate decide the
   same, both are right or both are wrong, so only rows where they **differ** can
   make one better than the other. On the example, that is 4 of 80 rows for
   `small-thinking`. Each changed row needs a verdict on both outcomes, and is
   *fixed* (candidate right, reference wrong), *broken* (the reverse) or a tie.

   ```
   improvement = (fixed - broken) / rows run by both
   ```

   An unreviewed change is unknown, not a tie, so the improvement stays unknown
   until every changed row is reviewed. Its 95% interval is a bootstrap over rows.

2. **The reference's error rate**, once per report. "10% fewer errors" needs to
   know how often the reference is wrong. It is measured on the **base sample**:
   `base_sample` golden rows drawn uniformly at random with the config's `seed`, so
   `caliper review` and `caliper eval` always draw the same rows. Every sampled row
   needs a verdict on the reference's outcome. Rows reviewed for any other reason
   (another candidate's changes, an earlier round) never enter this estimate,
   because they were not chosen at random. Its 95% interval is a Wilson score
   interval, which stays honest when the sample happens to hold no errors: 0 of 30
   still gives an upper end of 11.4%.

The **relative improvement** divides the two, conservatively: its low end is the
improvement's low end over the error rate's high end.

**Rows the reference did not decide** (usually provider errors) are left out of
both numbers and reported, so the reference's outage is never credited to a
candidate; `--retry-errors` re-runs them. The candidate's own missing decisions
count as errors, so it cannot abstain its way past a hard row.

**How big the base sample must be.** The relative low end divides by the error
rate's *high* end, and on 30 rows that is 1.5 to 3 times the estimate. A staff
review measured, on 4,000 rows with a 10% reference error rate: a candidate at
1.10x cost that truly makes 20% fewer errors fails with `base_sample = 30` and
passes with 300; one at 1.04x and 8% fewer errors passes only with 1,000. The
default of 30 is a cheap first look. To make a decision, plan on a few hundred
reviewed base-sample rows (growing `base_sample` keeps the rows already drawn, so
earlier verdicts stay useful). A base-sample row answered "Unsure" blocks the
error rate until someone decides it or adjudicates it.

**The gate.** `[ci] quality_per_cost = k`: a candidate that costs `ratio` times the
reference must make at least `k x (ratio - 1)` fewer errors, relative to the
reference's. With `k = 1`, 10% more cost needs at least 10% fewer errors, and
exactly 10% fewer is a pass. It passes when the relative improvement's low end
reaches that share and the improvement's low end is above 0.

```toml
[ci]
quality_per_cost = 1.0          # 10% more cost needs at least 10% fewer errors
quality_confidence = 0.95       # default; how sure the rule must be
quality_cost_tolerance = 0.02   # default; up to 1.02x counts as token noise, not extra cost
```

`quality_confidence` decides who carries the burden of proof. At 0.95 (the
default) it is on the more expensive candidate: one that is exactly as much better
as it costs more fails while there is real uncertainty, and passes once enough rows
are reviewed to show it. 0.8 is more lenient. 0 compares point estimates: the
candidate passes when its measured improvement reaches the bar, which also lets
through candidates that only look better by chance. The intervals in the report's
quality section use the same level.

`quality_cost_tolerance` exists because the cost ratio comes from token counts,
which vary from run to run. A candidate within it (1.02x by default) is not judged
by this rule.

It fails, with a next step, when a changed row is unreviewed, when the base sample
is not fully reviewed, or when the cost cannot be compared (no price). Paying more
for the same decisions never passes. Candidates that cost the same or less are not
judged by it; the regression gate still guards them. `max_cost_ratio`, if set,
stays a hard ceiling.

**Getting the verdicts.** `caliper review` lists the outcomes that still need one
and makes a `caliper label` project of them:

```bash
caliper eval --config caliper.toml --out build/eval                      # no --ci yet
caliper review --config caliper.toml --eval-out build/eval --out build/review-1
caliper label build/review-1                                              # a person: Yes / No / Unsure
caliper label export build/review-1 --format jsonl --out build/review-1/approvals.jsonl
# add it to [quality] approvals (review prints the exact line), then:
caliper eval --config caliper.toml --out build/eval --ci                  # resumes: no model calls
```

The review lists both outcomes of every changed row that has no verdict, plus the
reference's outcome on every base-sample row that has none. Each card shows the
request and one outcome, never the model, and the cards are shuffled. A card whose
row has no other card (a base-sample row, or a changed row whose other outcome was
judged earlier) can hint at which side produced it, never at which model. Verdicts
already given are not asked again, so a second round lists only what is new. Every
verdict in use is part of the test ledger's key, so the gated run after a review is
a new measurement, not a re-touch.

**Only fixes are free in the regression gate.** With `[quality]`, a change from an
outcome a person rejected to one a person approved costs 0 in `paired_excess` and
in `max_regression` slices, and the same rule applies to the reference's own replay
noise. A change between two acceptable outcomes still costs its `[costs]` weight:
an extra review is still an extra review. `n_fixes` counts the free changes.

**What an approval cannot see.** Caliper compares outcomes. If two candidates
reach the same outcome with different content behind it (the same `auto_publish`,
a different tax code), one verdict covers both. When that content matters, make it
part of the outcome (for example `auto_publish:CTX-104`).

On the example (`caliper.quality.toml`: the human golden labels plus 15 verdicts in
`approvals.jsonl`), the baseline's error rate on its 30-row base sample is 30.0%
[16.7%, 47.9%]. `small-thinking` costs 1.04x the baseline, so it must make at least
3.8% fewer errors. Of its 4 changed rows, 1 is fixed and 3 are broken: -8.3%
[-45.0%, +15.0%] of the baseline's errors, so the gate fails it.

### Label provenance: truth vs teacher agreement

A label is only as good as whoever produced it. Each golden row can say where
its `label` / `label_approved` came from (`label_source`) and at which step
(`label_stage`, free-form):

| `label_source` | is | the approved share among auto-handled rows is called |
| --- | --- | --- |
| `human_end_user` | human truth | **precision** |
| `internal_reviewer` | human truth | **precision** |
| `llm_teacher` | an LLM's verdict | **teacher agreement** |
| `heuristic` | a rule / weak label | **heuristic agreement** |
| (missing) | `unknown` | **agreement (unknown source)** |

Every label-based number (the calibration sweep and its kappa, the matched
operating point, slice precision) is reported **per source** and named by it.
Agreement with an LLM teacher is never called accuracy or precision, in the
terminal, in the markdown, or in `report.json`, whose rates use the neutral key
`auto_approved_rate` next to a `metric` field that names it. The report's
`labels` line counts rows per source. When no row has a human source it says
**NO HUMAN-TRUTH LABELS** and asks for a human-labelled slice (for example
through `caliper label`). A teacher that is fooled by the same thing as your
model agrees with it. That proves nothing about truth.

### Selection hygiene: pick on val, report on test, touch test once

A threshold chosen and measured on the same rows is optimistic. Give golden
rows a `split` (`train`, `val`, `test`):

- Thresholds (the calibration recommendation, the matched-operating-point
  threshold) are picked on `split = "val"` rows only, and reported on
  `split = "test"` rows. `train` rows are not used by either.
- With no `split` on any row, selection and reporting use every row, as before,
  and the report says `splits: none ... (optimistic)`. Rows without a split in
  a split golden set are used by transitions, the noise floor and
  `max_regression` slices, but not for picking or reporting thresholds.
- Transitions, the noise floor and the regression gate always use every row:
  nothing is selected there.

**The test ledger.** Every completed run that reports candidates on test rows
appends one line per candidate to `OUT/test_ledger.jsonl` (or `--test-ledger
PATH`): candidate name, `candidate_sha256` (the candidate settings),
`test_set_sha256` (id, request and labels of the test rows), the number of test
rows, whether it is the reference, and a UTC timestamp. The timestamp is only in
the ledger, so stdout and `report.json` stay byte-deterministic.

A **re-touch** is a non-reference candidate reported again with the same
settings on the same test set. It prints a warning on stderr. Under `--ci` it
fails that candidate unless you pass `--allow-test-retouch`. The reference is
recorded but never counts as a re-touch, because nothing is selected for it. A
changed candidate (model, params, ...) or a changed test row counts as a first
touch. An edited `decide` does not: re-checking test after tuning the decision
rule is exactly what the ledger exists to catch. `--no-resume` does not clear
the ledger. A CI job with a fresh `--out` starts with an empty one, so point
`--test-ledger` at a file you persist (commit it or cache it) to catch
re-touches across jobs. Golden sets with no `test` rows record nothing.

### Calibration of a confidence threshold

When golden rows carry `label_approved` (did a person approve the feature's
output for that row?), Caliper sweeps the rule "auto-accept when `score >= t`"
over `[calibration].thresholds`. The sweep reads the **selection rows**: `val`
rows when the golden set is split (all rows otherwise), and among those the
human-labelled rows when there are any (all labelled rows otherwise, named by
their source). For each threshold it reports:

- **auto / coverage**: rows that would be auto-accepted.
- **precision** (or teacher agreement, ...): share of auto-accepted rows the
  labels approve.
- **approved -> review**: approved rows that would still go to a person.
- **kappa**: Cohen's kappa between the rule and the approvals (from
  `caliper-kappa`). It is reported only when defined.

The report also gives a 10-bin reliability ECE. With `target_precision` set, it
recommends the **lowest threshold whose precision reaches the target** on the
selection rows. It then reports that threshold **on test, once per label
source**: precision for human rows, teacher agreement for `llm_teacher` rows,
and so on. Expect test to be lower than val: that gap is what picking on val
hides. The full sweep is printed for the first calibrated candidate (normally
the reference). Other candidates get their picked threshold and test numbers,
and `report.json` holds every sweep.

### Matched operating point

"The new model has 97% precision at 0.8 and the old one 95% at 0.75" mixes
score quality with how much each side automates. The matched operating point
compares them **at the same coverage**:

1. The reference's coverage on val is the share of val rows whose outcome is in
   `[operating_point].auto_outcomes` (auto-published, expensive call skipped,
   ...), which is the incumbent as deployed.
2. The candidate's threshold is the one among its distinct val scores whose
   coverage (`score >= t`) is closest to that. Ties go to the higher threshold. A
   `None` score (a fallback) is never auto-handled. A candidate with no scores
   is compared on its own outcomes. Labels are not used to pick the threshold.
3. On test, each side **loses** a labelled row when it is wrong about it: an
   approved row not auto-handled (*missed*) or an unapproved row auto-handled
   (*bad auto*). A row is **lost vs the reference** when the reference is right
   and the candidate is wrong.

Losses are reported per label source. Human rows give "loses", other sources
give "disagreements". Each source also gets a paired bootstrap CI of
`candidate lost - reference lost` per row. A `must_not_lose` slice (below)
requires zero rows lost vs the reference.

`label_approved` is a row-level verdict. The matched operating point measures
**routing** (was this row handled automatically when it should have been?),
not whether the candidate's content was right. In the example, a candidate
that auto-publishes a rare listing with a wrong tax code still counts as
handling it, because the label approved production's output for that row.
Put content into the outcome if you need it judged.

### Named slice gates

Aggregates hide hard cases. A `[[slices]]` entry names a set of rows and
gives it its own rule. A row belongs to a slice when the slice's name is in
`GoldenRow.slices`, or when it matches `where` (`{stratum = "lookalike"}`,
`{"metadata.family" = ["battery", "voucher"]}`; a list means "any of"). The
rules are:

| `rule` | `threshold` | passes when |
| --- | --- | --- |
| `max_regression` | required | the paired excess regression on the slice rows (same formula and bound as the main gate) is `<= threshold`. Uses every slice row. |
| `must_not_lose` | none | zero human-labelled slice rows **on test** are lost vs the reference at the matched operating point. Reports how many each side loses. Fails closed when no such row is on test. Needs `[operating_point]`. |
| `min_precision` | in [0, 1] | among slice rows on test that the candidate's own outcome auto-handles, the share a **human** approved is `>= threshold`. Fails closed with no human-labelled slice rows. Passes when nothing is auto-handled. Needs `[operating_point]`. |

`must_not_lose` and `min_precision` read human-labelled rows by default: a
loss, or a precision, is judged against truth. `label_sources = [...]`
chooses other sources. Their verdicts are then reported as disagreements or
agreement (for example "teacher agreement"), never as losses or precision.
Slices apply to every non-reference candidate. They are listed in the report
with a PASS/FAIL per candidate, and each failing slice becomes a gate reason
(`slice rare_code (must_not_lose): 7 human-labelled rows on test: baseline loses 0,
small-thinking loses 1; 1 lost vs baseline: lst-0053`).

### CI gate

With `--ci`, the run exits 1 when any candidate fails. A candidate fails when:

- **regression** (paired). For every row decided on the reference's replica 0
  and on the candidate:

  ```
  d_i    = cost(ref replica 0 -> candidate) - n_i
  n_i    = mean over reference replicas k >= 1 of (cost(r0 -> rk) + cost(rk -> r0)) / 2
           (0 when the row has no complete replays, or there is no noise floor)
  excess = mean(d_i), with a 95% paired percentile bootstrap CI
           (rows resampled once per draw; both terms on the same rows)
  ```

  `[ci].require = "non_inferiority"` (the default) fails when
  `excess.high > max_regression`: a pass means the candidate is no worse than
  the reference beyond its own replay noise plus `max_regression`, at 95%.
  `require = "no_evidence_of_regression"` fails only when
  `excess.low > max_regression`. A pass then only means "no detectable
  regression", which small golden sets give easily. The CI's width shrinks
  with the number of rows, so size `max_regression` to your golden set (the
  example uses 0.10 on 80 rows). This replaces the round-1 rule
  `regression.estimate > noise.high + max_regression`, which compared one
  candidate number with the upper end of a separate noise interval. With `[quality]`, a
  fix (rejected outcome to approved outcome) costs 0, in both terms.
- **slices**: any `[[slices]]` rule fails for it (one reason per slice);
- **cost** (with `[ci].max_cost_ratio`): its cost per row is above that
  multiple of the reference's, or either side has replies with no known price;
- **quality per cost** (with `[ci].quality_per_cost`): it costs more than the
  reference and human approvals do not show it that much better (see
  [quality against human approvals](#better-not-just-different-quality-against-human-approvals));
- **test re-touch**: its test split was already reported with the same
  settings (see [the test ledger](#selection-hygiene-pick-on-val-report-on-test-touch-test-once)),
  unless `--allow-test-retouch`;
- any reply was served by a model other than the one requested; or
- its error rate (results without a decision, over results) is above
  `[ci].max_error_rate` (default 0); or
- rows are missing on its side or the reference's; or
- its results are stale (recorded against a different golden request or candidate).

`--summary-md PATH` writes a short markdown summary (verdict, candidate table,
changed outcomes, calibration, gate failures) to post on the pull request:

```yaml
- name: Eval
  run: uv run caliper eval --config evals/caliper.toml --out build/eval --ci --summary-md build/eval/summary.md

- name: Post the summary
  if: always() && github.event_name == 'pull_request'
  run: |
    cat build/eval/summary.md >> "$GITHUB_STEP_SUMMARY"
    gh pr comment ${{ github.event.pull_request.number }} --body-file build/eval/summary.md
  env:
    GH_TOKEN: ${{ github.token }}
```

The job needs `permissions: pull-requests: write` to comment.

### Building a golden set: the stratified sampler

Production logs are dominated by the easy case. A uniform sample of 100 rows
from a log where the rare class is 3% of traffic tests it on about three rows.
`caliper sample` keeps the strata you name **whole** and samples the rest:

```bash
caliper sample --in logs.jsonl --out golden.jsonl \
    --stratum-field outcome --keep needs_review,fallback --negatives 40 \
    --id-field id --reference-field outcome
```

- `--stratum-field`, `--request-field` (default `request`), `--id-field` and
  `--reference-field` are dotted paths into each log line.
- The request is copied as logged. Log the exact request your feature sends,
  so replay is byte-faithful.
- `--reference-field` becomes the row's `reference_outcome`.
- `--cap-per-stratum N` bounds a kept stratum.
- `--seed` makes the pick reproducible.

Then add labels (`label`, `label_approved`, and `label_source` for who gave
them), a `split`, and `slices` where you have them.

## Golden set format

One JSON object per line (`caliper_eval.types.GoldenRow`, extra keys rejected):

```json
{"id": "lst-0042",
 "request": {"system": "...", "messages": [{"role": "user", "content": "..."}], "max_tokens": 400},
 "reference_outcome": "auto_publish",
 "label": "auto_publish",
 "label_approved": true,
 "label_source": "internal_reviewer",
 "label_stage": "final_review",
 "split": "test",
 "slices": ["rare_code"],
 "stratum": "rare_code",
 "metadata": {"anything": "you like"}}
```

Only `id` and `request` are required. `request` is provider-neutral, Anthropic
Messages-shaped, and is sent exactly as stored, apart from the candidate overlay.
`reference_outcome` records what production decided, for your own comparison.
`label_source` is one of `human_end_user`, `internal_reviewer`, `llm_teacher`
or `heuristic` (missing = `unknown`). `split` is `train`, `val` or `test`.
`slices` names the [slices](#named-slice-gates) the row belongs to.
The transitions use a fresh reference run.

## Config reference

TOML. Paths are relative to the config file.

```toml
golden = "golden.jsonl"           # required
decide = "decide.py:decide"       # "file.py:func" or "package.module:func"
reference = "baseline"            # candidate name; default: the first candidate
noise_replays = 2                 # reference replays for the noise floor (>= 1)
budget_usd = 2.0                  # optional hard cap
seed = 0                          # bootstrap seeds derive from this

[[candidates]]                    # one or more
name = "baseline"                 # unique
provider = "fake"                 # fake | anthropic | bedrock | bedrock_converse | openai_compat
model = "fake-large"
thinking = "off"                  # off | on
# thinking_budget_tokens = 2048   # older Anthropic models only
output_mode = "text"              # text | json_schema | tool
# output_schema = { ... }         # JSON Schema for json_schema / tool
replies_per_row = 3
params = {}                       # e.g. { effort = "low", max_tokens = 4000 }
# base_url = "..."                # openai_compat (and anthropic, to point at a proxy)
# region = "us-east-1"            # bedrock, bedrock_converse (also picks the AWS price list)
fake_script = "fake_models.py:respond"   # fake only

[pricing."fake-large"]            # USD per million tokens; overrides any provider price
input = 3.0
output = 15.0
# cache_read = 0.3                # default: input
# cache_write = 3.75              # default: input
# source = "where and when you checked"

[costs]                           # "from->to" = weight; unlisted changes cost 1.0
"auto_publish->needs_review" = 1.0

[ci]
max_regression = 0.10             # allowed paired excess (regression - replay noise)
require = "non_inferiority"       # or "no_evidence_of_regression" (see CI gate)
max_error_rate = 0.0
# max_cost_ratio = 1.25           # optional: fail above 1.25x the reference's cost per row
# quality_per_cost = 1.0          # optional, needs [quality]: X% more cost needs X% fewer errors
# quality_confidence = 0.95       # how sure the quality rule must be (0 = point estimates)
# quality_cost_tolerance = 0.02   # cost ratios up to 1.02x are not judged by it

# [quality]                       # optional: human approvals of outcomes
# approvals = ["approvals.jsonl"] # files from `caliper review` + `caliper label export`
# golden_labels = true            # human golden labels count as verdicts
# base_sample = 30                # random rows that measure the reference's error rate

# [cost_projection]               # optional: monthly cost per candidate in the report
# calls_per_day = 20000           # feature calls a day (golden-row equivalents)
# days_per_month = 30

[calibration]
thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
target_precision = 0.95           # optional: recommend a threshold (picked on val)

[operating_point]                 # optional: matched-operating-point comparison
auto_outcomes = ["auto_publish"]  # outcomes that count as "handled automatically"

[[slices]]                        # optional, repeatable: named slice gates
name = "rare_code"                # rows with "rare_code" in GoldenRow.slices ...
rule = "must_not_lose"            # max_regression | must_not_lose | min_precision
# threshold = 0.5                 # max_regression: allowed paired excess; min_precision: minimum
# where = { stratum = "rare_code" }          # ... or rows matching this ("metadata.key" too)
# label_sources = ["internal_reviewer"]      # which label sources a label rule reads
```

### Fake provider

`fake_script = "file.py:respond"` points at a function with this signature:

```python
def respond(request: dict, *, model: str, row_id: str, replica: int, sample: int) -> str | dict:
    ...
```

- `request` is the golden request with the candidate overlay merged in: `model`,
  `thinking` (`"on"` / `"off"`), `output_mode`, `output_schema` and `params`.
- Return the reply text, or a dict with `text`, `parsed`, `served_model`, `error`
  and `usage` (`input_tokens`, `output_tokens`, ...).
- Return a `served_model` different from `model` to rehearse a "who answered"
  mismatch.
- An optional module-level `supports(candidate: dict) -> str | None` returns a
  rejection reason for the preflight.
- Preflight calls `respond` with `row_id="__preflight__"`.
- Make the fake deterministic: derive randomness from a hash such as sha256 of
  the arguments. Never use Python's `hash()`, which is salted per process.

## CLI reference

```
caliper eval --config PATH [--out DIR] [--budget-usd X] [--no-resume] [--retry-errors]
             [--skip-preflight] [--ci] [--summary-md PATH] [--candidates a,b]
             [--target-precision P] [--max-error-rate R] [--n-boot N]
             [--test-ledger PATH] [--allow-test-retouch]
```

| flag | default | |
| --- | --- | --- |
| `--out` | `caliper-out` | output directory |
| `--budget-usd` | config `budget_usd` | override the cap |
| `--no-resume` | off | truncate `results.jsonl` and start fresh |
| `--retry-errors` | off | re-run rows whose provider call errored |
| `--skip-preflight` | off | skip the one-request-per-candidate check |
| `--ci` | off | exit 1 when the gate fails |
| `--summary-md` | none | write the PR-comment markdown |
| `--candidates` | all | comma-separated subset; the reference is always kept |
| `--target-precision` | `[calibration].target_precision` | threshold recommendation target |
| `--max-error-rate` | `[ci].max_error_rate` or 0 | gate error allowance |
| `--n-boot` | 1000 | bootstrap resamples |
| `--test-ledger` | `OUT/test_ledger.jsonl` | where test-split touches are recorded |
| `--allow-test-retouch` | off | do not fail `--ci` when a candidate's test split was already reported |

```
caliper review --config PATH --eval-out DIR --out NEW_DIR [--candidates a,b]
```

Writes `NEW_DIR/items.jsonl`: the outcomes still needing a human verdict (both
outcomes of every changed row, and the reference's outcome on the
`[quality] base_sample` rows), shuffled. With the `label` extra, `NEW_DIR` is also a
`caliper label` project. Free: no model calls. Exit 0, or 2 for bad input.

```
caliper sample --in LOGS --out GOLDEN --stratum-field FIELD --keep A,B --negatives N
               [--request-field request] [--id-field F] [--reference-field F]
               [--cap-per-stratum N] [--seed 0]
```

**Exit codes:**

| code | meaning |
| --- | --- |
| 0 | ok |
| 1 | gate failed (only with `--ci`) |
| 2 | usage or config error |
| 3 | preflight failed |
| 4 | budget exceeded (resume hint on stderr) |

The report goes to stdout, deterministically. Progress, notices, preflight
messages and test re-touch warnings go to stderr.

## Output files

| file | content |
| --- | --- |
| `OUT/results.jsonl` | one `RowResult` per (candidate, row, replica): replies (text, parsed, served_by, usage, latency, cost, error), the decision, and the request hash. Append-only; last line per key wins. |
| `OUT/report.json` | the full report: noise floor, label provenance (`provenance`), per-candidate tokens, latency p50/p95, dollars, served models, transitions, regression, `paired_excess`, calibration (`metric`, `on_report` per source), `matched_operating_point`, `slices`, `quality`, `approvals` and `base_rate` (with `[quality]`), warnings |
| `REVIEW/items.jsonl` | from `caliper review`: one outcome per line to judge (`id`, `row_id`, `outcome`, `request_sha256`, `request`) |
| `OUT/test_ledger.jsonl` | one line per candidate per completed run that reported test rows (timestamped; append-only; not cleared by `--no-resume`) |
| `--summary-md` | the markdown summary |

## Not in this release

There is no side-effect sandbox for evals that run near production code, and no
hosted dashboard. Results are files you keep in CI artifacts.
