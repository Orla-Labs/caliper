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
| `golden.jsonl` | 80 labeled `GoldenRow`s built from those logs. It keeps every rare, mixed, lookalike and unrecognised listing, plus 30 common ones. Each row holds the request as sent, `reference_outcome` (what production decided), and `label_approved` (did a person approve production's output). |
| `decide.py` | The feature's logic as `decide(replies) -> Decision`. This is what Caliper grades. |
| `feature_rule.py` | The same rule as production code would write it: raw strings in, its own action names out. |
| `tests/test_parity.py` | `assert_parity` proves `decide.py` matches `feature_rule.py` on every sequence of 0 to 3 replies from a 12-shape alphabet. The alphabet covers a score exactly at the threshold, `true` and `NaN` confidences, truncated JSON, errors and parsed-only replies. A second test shows an off-by-one mirror gets caught. |
| `fake_models.py` | The deterministic fake provider: `respond(...)` plus a `supports(...)` preflight. |
| `caliper.toml` | Offline eval: `baseline` (fake-large, the reference), `small` (fake-small), `small-thinking` (fake-small with thinking on). |
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

## Run it

From the repo root:

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
golden set has 80 rows. A person approved production's output on 67 of them.

**Preflight.** `small-thinking-schema` asks for `json_schema` output with thinking on. The
fake provider refuses that shape, so `caliper eval` exits 3 before running a single row.

**Noise floor.** The baseline is replayed twice. 1 of 80 rows (1.2%) changes
outcome between the two replays, with a cost-weighted flip rate of 0.037 (95% bootstrap CI 0.000 to
0.113). Differences below that line are noise, not regressions.

**Transitions vs the baseline, cost-weighted.** In `[costs]`, a record that newly needs a
person costs 1. A record that goes live without the check it would have had costs 5.

| candidate | flips | regression [95% CI] | cost $ (fake prices) | gate |
| --- | ---: | --- | ---: | --- |
| `baseline` (ref) | - | - | 0.6068 | PASS |
| `small` | 20/80 | 0.300 [0.175, 0.463] | 0.0799 | **FAIL** |
| `small-thinking` | 4/80 | 0.050 [0.013, 0.113] | 0.3150 | PASS |

`small` changes 20 outcomes: 14 rare-family listings (every `rare_code` row), 4
lookalikes whose misleading keyword it does not know, and 2 mixed listings. Of
those 20, 19 are records that used to publish automatically and now need a person. `small-thinking` stays inside the
noise floor plus the 0.02 allowance, at about half the baseline's cost.

**Calibration of the 0.75 threshold.** Against human approvals, the baseline's
auto-published records at score 0.75 or more are 84.4% approved, at 80% coverage. The
lookalikes are confidently wrong. With `target_precision = 0.95`, Caliper
recommends 0.85: 100% precision at 63.7% coverage. The cost of that safety is
visible: 23.9% of approved records would go to a person.

**CI verdict.** `caliper eval --ci` exits 1 because `small` fails. `summary.md` is
the markdown a CI step posts on the pull request.

## What this example does not show

* **A confidently wrong code that still auto-publishes.** Transitions are computed on the routing
  outcome (publish / review / fallback). The tax code lives in `Decision.detail`.
  If a candidate publishes `CTX-510` where the reference published `CTX-520`,
  both outcomes are `auto_publish`. To see code changes in the matrix, put the code in
  the outcome (for example `auto_publish:CTX-520`) and list the costs you care about.
* **Real-world accuracy.** The fake provider is a simulation built to exercise the
  method. Run `caliper.real.toml` to measure your own models.
