# catalog_cleanup: is it safe to switch to a cheaper model?

A worked example of `caliper eval`. Everything here is synthetic. The shops,
brands and products are invented, the CTX tax-category codes are a made-up
scheme, and every number comes from the fake provider in this folder, not from
a real system.

## The feature

An online marketplace receives raw supplier listings like this one:

```
Supplier: Northbeam Trading Co. (fictional)
Ref: NB-4438
Item: Moonwick cookbook with spice sampler | pages: 180 | pack: 6
Notes: carton dims on request
```

An LLM turns each listing into a catalog record: a short description, the
specs, a tax-category code from a small list (`CTX-100` general merchandise,
`CTX-310` books, `CTX-520` loose batteries, `CTX-900` gift cards, ...), and a
confidence score. The feature does not trust a single reply:

1. It asks three times.
2. If fewer than two replies parse, the record goes to manual entry (`fallback`).
3. If two or more replies agree on the code, the score is their mean confidence.
   If they disagree, the record goes to a person (`needs_review`).
4. A score of 0.75 or more publishes automatically (`auto_publish`). Anything lower goes to a person (`needs_review`).

The team wants to replace the model behind it with a cheaper one. The question
is not "does the small model answer like the big one". It is "would the
*feature* have done the same thing, and what does each difference cost?"

## Files

| File | What it is |
| --- | --- |
| `logs.jsonl` | 300 rows of a fake production log: the exact request sent, the outcome, the code, and the reviewer verdict where a person looked. Rare product families are a few percent of traffic. |
| `golden.jsonl` | 80 labeled `GoldenRow`s built from those logs. It keeps every rare, mixed, lookalike and unrecognised listing, plus 30 common ones. Each row holds the request as sent, `reference_outcome` (what production decided), `label_approved` (was production's output approved), `label_source` (who said so), `split` (`val` or `test`) and `slices` (`rare_code`, `lookalike`). |
| `decide.py` | The feature's logic as `decide(replies) -> Decision`. This is what Caliper grades. |
| `feature_rule.py` | The same rule as production code would write it: raw strings in, its own action names out. |
| `tests/test_parity.py` | `assert_parity` proves `decide.py` matches `feature_rule.py` on every sequence of 0 to 3 replies from a 12-shape alphabet. The alphabet covers a score exactly at the threshold, `true` and `NaN` confidences, truncated JSON, errors and parsed-only replies. A second test shows an off-by-one mirror gets caught. |
| `fake_models.py` | The deterministic fake provider: `respond(...)` plus a `supports(...)` preflight. |
| `caliper.toml` | Offline eval: `baseline` (fake-large, the reference), `small` (fake-small), `small-thinking` (fake-small with thinking on). Also the paired gate allowance, the operating point and two named slices. |
| `caliper.preflight.toml` | A candidate shape the fake provider rejects (fake-small, thinking on and `json_schema` output). Preflight stops it before any spend. |
| `caliper.real.toml` | The same golden set on real providers: Claude Sonnet 5.5 as baseline, Claude Haiku 5.5 with thinking off and on, and an OpenAI-compatible endpoint. Needs keys and costs money. Not run in CI. |
| `generate.py` | Regenerates both JSONL files deterministically. |
| `expected_output.txt` | Byte-exact transcript of `scripts/demo_eval.sh`. |

### How the fake models behave

* **`fake-large`** knows every product family. Listings that mention two
  families ("kids rain jacket", "herbal tea gift set with ceramic mug") get
  mid-range confidence that is a property of the listing, plus a little jitter
  per sample. That jitter is the baseline's own replay noise. Lookalike listings
  ("gift card holder wallet, card not included") fool it with fairly high
  confidence.
* **`fake-small`** does not know the rare families: loose batteries, home health
  devices, gift cards. On those it guesses a nearby code with low, scattered
  confidence. It sometimes returns truncated JSON.
* **`fake-small` with thinking on** gets the rare family right on most of those
  listings, and spends extra output tokens doing it.

Every random-looking choice comes from a sha256 digest of (model, thinking,
row, replica, sample), never from Python's `hash()`. Runs are byte-identical.

### Who labelled what

* **63 rows by an internal reviewer** (human truth). Every rare-code and
  unrecognised listing is among them.
* **13 rows by an LLM teacher.** The teacher is fooled by the same misleading
  keyword as the production model, so it "approves" every lookalike it labels.
  Its numbers are reported as *teacher agreement*, never as precision.
* **4 common rows with no recorded source**, shown as `unknown`.

Within each stratum, rows alternate between `val` (40) and `test` (40), so
every hard case appears on both sides. Thresholds are picked on val and
reported on test.

## Run it

After `pip install orlalabs-caliper` and `caliper example catalog_cleanup`, from the folder
that holds the copy:

```bash
caliper sample --in catalog_cleanup/logs.jsonl --out catalog_cleanup/out/sampled.jsonl \
    --stratum-field outcome --keep needs_review,fallback --negatives 40 \
    --id-field id --reference-field outcome
caliper eval --config catalog_cleanup/caliper.toml --out catalog_cleanup/out \
    --ci --summary-md catalog_cleanup/out/summary.md
```

From a clone of the repo, at its root:

```bash
scripts/demo_eval.sh            # the whole story, about a second
scripts/demo_eval.sh --check    # byte-exact against expected_output.txt

# the pieces
uv run caliper sample --in examples/catalog_cleanup/logs.jsonl --out build/sampled.jsonl \
    --stratum-field outcome --keep needs_review,fallback --negatives 40 \
    --id-field id --reference-field outcome
uv run caliper eval --config examples/catalog_cleanup/caliper.toml --out build/catalog-eval \
    --ci --summary-md build/catalog-eval/summary.md
uv run pytest -q examples/catalog_cleanup/tests
uv run python examples/catalog_cleanup/generate.py    # regenerate the data (optional)
```

## What the demo shows (synthetic numbers)

**Golden set.** `caliper sample` keeps all 16 rows production sent to review and samples 40 of
the 284 auto-published rows. Rare classes survive the sampling. The labeled
golden set has 80 rows: 63 labelled by an internal reviewer, 13 by an LLM
teacher and 4 with no recorded source, split 40 val / 40 test.

**Preflight.** `small-thinking-schema` asks for `json_schema` output with thinking on. The
fake provider refuses that shape, so `caliper eval` exits 3 before running a single row.

**Noise floor.** The baseline is replayed twice. 1 of 80 rows (1.2%) changes
outcome between the two replays, a cost-weighted flip rate of 0.037.

**The aggregate gate, paired.** In `[costs]`, a record that newly needs a person costs 1. A
record that goes live without the check it would have had costs 5. Row by row, the
gate subtracts the baseline's own replay cost from the candidate's cost and
bootstraps the difference. It passes a candidate only when the upper end of that
95% interval is within `max_regression = 0.10`.

| candidate | flips | regression [95% CI] | excess vs noise (paired) | $/row (x ref, fake prices) | gate |
| --- | ---: | --- | --- | ---: | --- |
| `baseline` (ref) | - | - | - | 0.003792 | PASS |
| `small` | 20/80 | 0.300 [0.175, 0.463] | 0.263 [0.163, 0.362] | 0.000998 (0.26x) | **FAIL** |
| `small-thinking` | 4/80 | 0.050 [0.013, 0.113] | 0.013 [-0.087, 0.087] | 0.003937 (1.04x) | **FAIL** (slice) |

`small` changes 20 outcomes, 19 of which are records that used to publish
automatically and now need a person. `small-thinking` is non-inferior overall:
its upper bound, 0.087, is within 0.10. It is not cheaper, though: the cheaper
model's thinking tokens bring it to 1.04x the baseline's cost per row, about
+$87 a month at the 20,000 calls a day in `[cost_projection]`. (The run's raw
dollars, 0.3150 vs 0.6068, look like half, but the baseline was replayed twice
for the noise floor; cost per row divides that out.) `[ci] max_cost_ratio = 1.25`
lets it through on cost. The
allowance is 0.10 rather than 0.02 because on 80 rows the paired interval is
about ±0.09 wide. A tighter allowance needs a bigger golden set.

**Named slices.** `rare_code` is *must not lose*. On test, at the matched
operating point, the baseline loses none of the 7 reviewer-labelled rare-code
rows, and `small` loses 4. That is a floor: some of the other 3 clear the
matched threshold with a tax code `small` guessed wrong, which the routing
labels cannot see (see [below](#what-this-example-does-not-show)). `small-thinking` loses 1 (`lst-0053`, a blood-pressure cuff it scores 0.61). That
single record blocks it, even though the aggregate gate passes. `lookalike`
allows a paired excess of up to 0.5. `small` exceeds it (upper bound 0.875);
`small-thinking` does not move a single lookalike.

**Matched operating point.** The baseline auto-publishes 77.5% of val rows.
`small-thinking` matches that coverage at score >= 0.807. On the 35 test rows
labelled by a reviewer it loses 11 rows to the baseline's 10. The paired
delta is +0.029 [-0.057, +0.114]: it is not shown to be worse, nor better. On
the 4 teacher-labelled test rows the report only talks about disagreements with
the teacher.

**Calibration, picked on val, reported on test.** On the 28 reviewer-labelled
val rows, the baseline reaches 95.2% precision at score 0.70, so that is the
recommendation for `target_precision = 0.95`. On test, the same threshold
gives **85.7% precision** (reviewer rows). The lookalikes that are confidently
wrong pull it down, and val was optimistic. On the teacher's rows, the same
threshold shows 75.0% *teacher agreement*.

**Test ledger.** The run records one test touch per candidate in
`build/catalog-eval/test_ledger.jsonl`. Re-running the same candidates on the
same test set with `--ci` fails unless you pass `--allow-test-retouch`. The demo
starts from an empty `build/` each time, so its output never changes.

**CI verdict.** `caliper eval --ci` exits 1: `small` fails the aggregate gate
and both slices, and `small-thinking` fails the `rare_code` slice. `summary.md`
is the markdown a CI step posts on the pull request.

## What this example does not show

* **A confidently wrong code that still auto-publishes.** Transitions are computed on the routing
  outcome (publish / review / fallback). The tax code lives in `Decision.detail`.
  If a candidate publishes `CTX-510` where the reference published `CTX-520`,
  both outcomes are `auto_publish`. To see code changes in the matrix, put the code in
  the outcome (for example `auto_publish:CTX-520`) and list the costs you care about.
* **Content correctness in the matched operating point.** `label_approved`
  says whether production's output for that row was approved. A candidate
  that auto-publishes a rare listing with a *different* code counts as
  handling it. The operating point measures routing, not the code.
* **Real-world accuracy.** The fake provider is a simulation built to exercise the
  method. Run `caliper.real.toml` to measure your own models.
