# `caliper eval`

`caliper eval` answers one question for an LLM feature: *if we ship this model or
prompt change, what will the feature do differently, and is that worse than
the noise we already live with?* It replays a golden set of real requests
through each candidate, applies your feature's own decision logic, and compares
the outcomes to a reference run, with costs and a CI gate attached.

The worked example is [`examples/catalog_cleanup`](../examples/catalog_cleanup/README.md).
`scripts/demo_eval.sh` runs it offline in about a second.

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

It raises `ParityError` on the first counterexample, with the sequence and both
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

A candidate's regressions only count above the *upper* bound of this interval.

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
exceed 1, and it gets a bootstrap CI like the noise floor.

Transitions are only as fine-grained as your outcome. If the outcome is
`auto_publish` and the detail that changed (say, the tax code) lives in
`detail`, the matrix will not show it. Put it in the outcome string when you
need it.

### Candidates: model + thinking + output mode

A candidate is the whole request shape, not just a model name:

| field | meaning |
| --- | --- |
| `provider` | `fake`, `anthropic` (direct), `bedrock` (Anthropic models on Amazon Bedrock), `openai_compat` (any chat-completions endpoint) |
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

### Preflight and who answered

Before touching the golden set, `caliper eval` sends one small request per
candidate. A shape the provider rejects fails in seconds with the provider's
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
- Run IDs are derived from the config and golden content. There are no timestamps
  anywhere, so fake-provider output is byte-identical across runs and machines.

### Pricing caveats

Costs come from a cache-aware table in USD per million tokens. Input, output,
cache read and cache write are priced separately.

- Anthropic first-party models are built in, copied from the
  [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) on
  2026-11-06. Prices change, so check before trusting the dollar column.
- `[pricing."<model>"]` in the config overrides or adds a model.
- Bedrock model IDs are priced at the first-party list price as an approximation,
  with a warning. Bedrock sets its own prices.
- No OpenAI-compatible prices are built in. Add your own. For those endpoints,
  cached prompt tokens are subtracted from input and billed as cache reads.
- A model with no known price costs $0 and is flagged in the report.

Dollars and tokens include every replica, so the reference's noise replays are
counted.

### Calibration of a confidence threshold

When golden rows carry `label_approved` (did a person approve the feature's
output for that row?), Caliper sweeps the rule "auto-accept when `score >= t`"
over `[calibration].thresholds`. For each threshold it reports:

- **auto / coverage**: rows that would be auto-accepted.
- **precision**: share of auto-accepted rows a person approved.
- **approved -> review**: approved rows that would still go to a person.
- **kappa**: Cohen's kappa between the rule and the approvals (from
  `caliper-kappa`). It is reported only when defined.

The report also gives a 10-bin reliability ECE. With `target_precision` set, it
recommends the **lowest threshold whose precision reaches the target**. That is
the most automation you can have at the precision you asked for. Calibration is
computed per candidate, so you can see whether a new model's scores mean the
same thing as the old one's.

### CI gate

With `--ci`, the run exits 1 when any candidate fails. A candidate fails when:

- its cost-weighted regression estimate is above the noise floor's upper CI bound
  plus `[ci].max_regression`; or
- any reply was served by a model other than the one requested; or
- its error rate (results without a decision, over results) is above
  `[ci].max_error_rate` (default 0); or
- rows are missing on its side or the reference's.

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
uv run caliper sample --in logs.jsonl --out golden.jsonl \
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

Then add labels (`label`, `label_approved`) where you have them.

## Golden set format

One JSON object per line (`caliper_eval.types.GoldenRow`, extra keys rejected):

```json
{"id": "lst-0042",
 "request": {"system": "...", "messages": [{"role": "user", "content": "..."}], "max_tokens": 400},
 "reference_outcome": "auto_publish",
 "label": "auto_publish",
 "label_approved": true,
 "stratum": "rare_code",
 "metadata": {"anything": "you like"}}
```

Only `id` and `request` are required. `request` is provider-neutral, Anthropic
Messages-shaped, and is sent exactly as stored, apart from the candidate overlay.
`reference_outcome` records what production decided, for your own comparison.
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
provider = "fake"                 # fake | anthropic | bedrock | openai_compat
model = "fake-large"
thinking = "off"                  # off | on
# thinking_budget_tokens = 2048   # older Anthropic models only
output_mode = "text"              # text | json_schema | tool
# output_schema = { ... }         # JSON Schema for json_schema / tool
replies_per_row = 3
params = {}                       # e.g. { effort = "low", max_tokens = 4000 }
# base_url = "..."                # openai_compat (and anthropic, to point at a proxy)
# region = "us-east-1"            # bedrock
fake_script = "fake_models.py:respond"   # fake only

[pricing."fake-large"]            # USD per million tokens; overrides the built-in table
input = 3.0
output = 15.0
# cache_read = 0.3                # default: input
# cache_write = 3.75              # default: input
# source = "where and when you checked"

[costs]                           # "from->to" = weight; unlisted changes cost 1.0
"auto_publish->needs_review" = 1.0

[ci]
max_regression = 0.02             # allowed regression above the noise floor's upper bound
max_error_rate = 0.0

[calibration]
thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
target_precision = 0.95           # optional: recommend a threshold
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

The report goes to stdout, deterministically. Progress, notices and preflight
messages go to stderr.

## Output files

| file | content |
| --- | --- |
| `OUT/results.jsonl` | one `RowResult` per (candidate, row, replica): replies (text, parsed, served_by, usage, latency, cost, error), the decision, and the request hash. Append-only; last line per key wins. |
| `OUT/report.json` | the full report: noise floor, per-candidate tokens, latency p50/p95, dollars, served models, transitions, regression, calibration, warnings |
| `--summary-md` | the markdown summary |

## Not in this release

There is no side-effect sandbox for evals that run near production code, and no
hosted dashboard. Results are files you keep in CI artifacts.
