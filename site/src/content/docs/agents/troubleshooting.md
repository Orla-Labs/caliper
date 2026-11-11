---
title: "Errors and environment"
description: "Caliper's error messages with their cause and fix, the outcomes that look like failures but are not, and every environment variable Caliper reads."
---

Errors go to stderr as one line (sometimes followed by a hint), prefixed with the command
(`caliper eval: ...`). Match on the stable part of the message shown below.

## Errors

| Exit | Message (stable part) | Cause | Fix |
| --- | --- | --- | --- |
| 2 | `caliper eval: [Errno 2] No such file or directory: '...'` | The config path does not exist. | Pass the right `--config`. Paths inside the config resolve against its folder. |
| 2 | `validation error for EvalConfig` + `Extra inputs are not permitted` | An unknown or misspelt key (the line above names it, e.g. `candidates.1.temprature`). | Fix the key; model settings go in the candidate's `params = { ... }`. See the [config schema](/agents/files/#calipertoml). |
| 2 | `config has no 'decide' (expected 'file.py:func')` | `decide` is missing from the config. | Add `decide = "decide.py:decide"` ([recipe 4](/agents/recipes/#4-write-the-decide-function)). |
| 2 | `golden.jsonl:N: invalid golden row` | Line `N` is not a valid golden row (the next lines say which field). | Each line needs `id` and `request`; see the [golden-row schema](/agents/files/#goldenjsonl). |
| 2 | `unknown candidate(s) [...]; config has [...]` | `--candidates` names a candidate not in the config. | Use names from the config. |
| 3 | `preflight failed for NAME: ... needs the SDK: pip install 'orlalabs-caliper[EXTRA]'` | The provider's extra is not installed. | Run the `pip install` line from the message. |
| 3 | `preflight failed for NAME: <provider> rejected the request shape (...)` + the provider's error | The provider refused the candidate's shape: an unsupported thinking setting, output mode or schema, a wrong model id, missing access or credentials. | Read the provider's message; change the candidate. Do not pass `--skip-preflight` to get past it: every row would fail the same way, and be paid for. |
| 3 | `caliper eval: cannot run candidates: ...` | A provider could not be built at run time (usually a missing extra). | As above. |
| 4 | `budget exceeded: spent $X + next row ~$Y > limit $Z` | The budget cap stopped the run. Results so far are kept. | Re-run the same command to resume; raise `--budget-usd` only with your user's agreement. |
| 1 | `test split re-touched: NAME was already reported on this test set Nx` | A `--ci` run into an `--out` whose ledger already has this candidate and test set. | Use a fresh `--out`. If re-reporting is intended, `--allow-test-retouch`. |
| 1 | `cost unknown: N replies priced at $0 for MODEL (no price found)` | `[ci] max_cost_ratio` is set and a candidate has no price. | Add `[pricing."MODEL"]` ([recipe 5](/agents/recipes/#5-price-a-model-caliper-cannot-price)). |
| 1 | `N results recorded for a different request or candidate settings than the current config` | `OUT/results.jsonl` holds results from an older config or golden set. | Re-run with `--no-resume`, or use a fresh `--out`. |
| 1 | `N replies served by a model other than requested` | The provider answered with another model (a fallback or alias). | Request the exact model id the provider reports; check the `served_models` field. |
| 1 | `error rate X% > allowed Y%` | Replies failed or `decide()` raised. | Look at `error` in `OUT/results.jsonl`; `--retry-errors` re-runs provider failures; fix `decide()` for decision errors, or set `[ci] max_error_rate`. |
| 1 | `N rows missing on one side (incomplete run)` | The run did not finish for a candidate. | Re-run the same command to complete it. |
| 2 | `caliper label needs the label extra (missing module '...')` | `caliper label` without the extra. | `pip install 'orlalabs-caliper[label]'`. |
| 2 | `caliper example: DIR already exists; pick another directory` | The destination exists. | Pass a new directory as the second argument. |
| 2 | `error: --on-error none is only valid for --target trl` | `caliper-compile` option mismatch. | Use a float, or `--target trl`. |

## Not errors

| What you see | Meaning |
| --- | --- |
| `caliper eval` exits `0` and a candidate shows `FAIL` | Without `--ci` the gate is only printed. Pass `--ci` for the exit code. |
| `caliper example catalog_cleanup` then `--ci` exits `1` | Expected: the example's cheaper candidates fail on purpose. |
| `warning: pricing NAME: MODEL: priced from Caliper's built-in Anthropic list-price table` | Anthropic has no pricing API; prices come from a dated table. Add `[pricing]` to be exact. |
| `unpriced x240` in the `$/row` column | No price for that model. Cost shows `$0`. See recipe 5. |
| `note: added reference '...' to --candidates` | The reference always runs; it is the baseline for noise and transitions. |
| An eval finishes instantly with no new spend | Resume found every row already in `OUT/results.jsonl`. |

## Environment variables

| Variable | Read by | Effect |
| --- | --- | --- |
| `CALIPER_PRICING_OFFLINE` | `caliper eval` | `1`: never fetch AWS price lists or model names; use only the cache. |
| `CALIPER_CACHE_DIR` | `caliper eval` | Cache folder for AWS price lists and Bedrock model names (default `~/.cache/caliper`, refreshed after 24 h). |
| `AWS_REGION`, `AWS_DEFAULT_REGION` | `caliper eval` | Region for Bedrock pricing when the candidate sets no `region`. |
| `ANTHROPIC_API_KEY` | `provider = "anthropic"` | Anthropic API key (read by the Anthropic SDK). |
| `AWS_PROFILE` and the AWS credential chain | `bedrock`, `bedrock_converse`, label S3 sources | AWS credentials. |
| `OPENAI_API_KEY`, or the name in `params.api_key_env` | `provider = "openai_compat"` | API key for the endpoint. |
| `CALIPER_LABEL_TOKEN` | `caliper label --auth token` | The access token (default: generated and printed). |
| `CALIPER_LABEL_HOME` | `caliper label` | App state: recent projects and preferences (default `~/.caliper-label`). |
| `CALIPER_LABEL_PROJECTS` | `caliper label` | Default parent folder for new projects (default `~/caliper-projects`). |
| `CALIPER_LABEL_DEMO` | `caliper label` | Folder of the demo project shown on the start screen. |
| `CALIPER_LABEL_MAX_UPLOAD` | `caliper label` | Largest file the setup wizard accepts, in bytes (default 2 GiB). |
| `CALIPER_LABEL_DUCKDB_MEMORY_LIMIT` | `caliper label` | DuckDB memory limit for reading sources (default `384MB`). |
| `CALIPER_LABEL_DUCKDB_THREADS` | `caliper label` | DuckDB threads (default `2`). |
| `CALIPER_LABEL_DUCKDB_TEMP_DIR` | `caliper label` | DuckDB spill folder (default a `caliper-label-duckdb` folder in the system temp dir). |
| `CALIPER_LABEL_DUCKDB_EXTENSION_DIR` | `caliper label` | Where DuckDB keeps its extensions (for offline machines). |
| `CALIPER_LABEL_DUCKDB_EXTENSION_REPO` | `caliper label` | A mirror of the DuckDB extension repository. |
| the name in a project's `credentials.env` | `caliper label` | DSN or token for that project's data source; the project file stores only the name. |
| `CALIPER_STORE_URL` | `caliper-server` | SQLAlchemy URL of the eval-set store (unset: in memory). |
| `CALIPER_GRADER_EXECUTOR` | `caliper-server` | `in_process` (default) or `e2b` (needs `E2B_API_KEY` and the `e2b` extra). |

## Network access

`caliper eval` reaches the network only for:

- the providers of non-`fake` candidates;
- the public AWS Price List (`pricing.us-east-1.amazonaws.com`) and Bedrock
  `GetFoundationModel`, when a Bedrock candidate needs a price.

`caliper label` reaches whatever the project's data source is, and downloads DuckDB
extensions for remote sources on first use. Nothing else in Caliper uses the network, and
nothing sends telemetry.
