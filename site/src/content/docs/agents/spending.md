---
title: "What costs money"
description: "Which Caliper commands make paid API calls, how to estimate a run before starting it, the guard rails, and the credentials each provider reads."
---

Caliper never charges anything itself. Money is spent only when `caliper eval` sends
requests to a model provider on your user's account.

## Rules for agents

1. **Ask before any run with a non-`fake` candidate.** Say which candidates, how many rows,
   and your cost estimate. A run with only `provider = "fake"` candidates (and
   `caliper example`) is free and offline; run those without asking.
2. **Always set `budget_usd`** in the config (or `--budget-usd`). Never raise it, or
   re-run a budget-stopped eval with a higher one, without your user's agreement.
3. **Pilot first.** Run 10 to 20 rows, read `cost_per_row`, then estimate the full run.
4. **Don't loop on paid runs.** A config error exits `2` before anything is sent, and a
   rejected request shape exits `3` after one small request per candidate. Fix the cause
   instead of retrying.

## What each command touches

| Command | Paid calls | Network | Notes |
| --- | --- | --- | --- |
| `caliper eval` (only `fake` candidates) | none | none | free and offline |
| `caliper eval` (real providers) | **yes** | provider APIs, AWS price list (public), Bedrock `GetFoundationModel` | preflight + golden set |
| `caliper eval --skip-preflight` | **yes** | as above | skips the one-request check; a bad shape then fails on every row |
| `caliper sample` | none | none | local files |
| `caliper example` | none | none | copies bundled files |
| `caliper kappa` | none | none | runs your grader file locally |
| `caliper label`, `label serve` | none | the project's data source (may be S3, GCS, HTTP or a database) | local web UI |
| `caliper label export` | none | S3 or the source database with `--out s3://...` / `--table` | |
| `caliper-compile` | none | none | writes training files; submitting a job is up to you |
| `caliper-server` | only with `CALIPER_GRADER_EXECUTOR=e2b` (E2B sandboxes on your E2B account) | listens locally; E2B with that setting | |

## What one eval sends

For each candidate, in order:

1. **Preflight:** one small request with the candidate's exact shape (model, thinking,
   output mode, schema), built from the first golden row. A rejected shape stops the run
   with exit `3` before the golden set is touched.
2. **The golden set:** every row, `replies_per_row` requests per row. The reference runs
   `noise_replays` times (default 2) to measure its own noise; other candidates run once.

Requests per run:

```
preflight = number of candidates
golden    = rows × replies_per_row × (noise_replays for the reference, 1 for each other candidate)
```

Example: 200 rows, a reference and two candidates, 3 replies per row, `noise_replays = 2`
gives `3 + 200 × 3 × (2 + 1 + 1) = 2,403` requests.

## Estimate before you run

Run a pilot on a slice of the golden set ([Recipes, step 4](/agents/recipes/#1-is-it-safe-to-switch-to-a-cheaper-model)),
then for each candidate:

```
estimated $ = cost_per_row × rows × executions
              (executions = noise_replays for the reference, 1 otherwise)
```

```bash
jq -r '.candidates[] | "\(.name)\t\(.cost_per_row)"' build/pilot/report.json
```

`cost_per_row` is per execution, so it already includes `replies_per_row`. Outputs vary
in length, so add a margin (20% is reasonable) and set `budget_usd` to the result.

## The guard rails

- **`budget_usd` is a hard cap.** Before each row Caliper estimates the row's cost from
  the candidate's mean so far and stops with exit `4` rather than exceed the cap. The
  estimate can overshoot by one row on a candidate's first row. Only spend from the
  current invocation counts; re-running resumes with a fresh budget for the rest.
- **Resume never re-pays.** Finished rows in `OUT/results.jsonl` are skipped on a re-run.
  `--no-resume` throws them away and pays again; use it only when results are stale.
- **`--candidates a,b`** runs a subset (the reference is always added, because the noise
  floor and transitions need it).

## Credentials each provider reads

Caliper stores no credentials. Each provider SDK reads its usual environment:

| Provider | Extra | Credentials |
| --- | --- | --- |
| `fake` | none | none |
| `anthropic` | `anthropic` | `ANTHROPIC_API_KEY` |
| `bedrock` | `bedrock` | the AWS credential chain (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, SSO, instance role); region from the candidate's `region`, else `AWS_REGION` |
| `bedrock_converse` | `bedrock-converse` | the AWS credential chain, as above |
| `openai_compat` | none | the env var named by `params.api_key_env` (default `OPENAI_API_KEY`) |

Bedrock pricing also calls `bedrock:GetFoundationModel` to match a model id to its
price-list name. Without that permission (or with `CALIPER_PRICING_OFFLINE=1` and an
empty cache) the model has no live price: Claude models fall back to Anthropic's list
price with a warning, other models are reported as unpriced.
