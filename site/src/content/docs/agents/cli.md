---
title: "CLI reference"
description: "Every Caliper command and flag, with exit codes and what each command touches (files, network, money)."
editUrl: false
---

:::note
Generated from the code by `scripts/gen_agent_docs.py`; edit the code, then re-run it.
:::

Conventions for every command:

- stdout is deterministic (no absolute paths, timestamps or run ids); progress and notices go to stderr.
- argparse itself exits `2` on an unknown flag or a missing required one.
- Paths inside a config file resolve against the config file's folder, not the working directory.
- Machine-readable output: `caliper eval` writes `OUT/report.json` ([schema](/schemas/eval-report.schema.json)); `caliper kappa --json` prints JSON.

## `caliper eval`

run candidates over a golden set and report

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--config` `CONFIG` | yes |  | eval TOML file |
| `--out` `OUT` |  | `caliper-out` | output dir (default: caliper-out) |
| `--budget-usd` `BUDGET_USD` |  |  | override budget_usd |
| `--no-resume` |  |  | start the results file fresh |
| `--retry-errors` |  |  | re-run rows that errored |
| `--skip-preflight` |  |  | skip the one-request-per-candidate shape check before the golden set |
| `--ci` |  |  | exit 1 when the gate fails |
| `--summary-md` `SUMMARY_MD` |  |  | write a PR-comment markdown summary |
| `--candidates` `CANDIDATES` |  |  | comma-separated subset to run |
| `--target-precision` `TARGET_PRECISION` |  |  | recommend the lowest threshold reaching this precision (default: [calibration].target_precision) |
| `--max-error-rate` `MAX_ERROR_RATE` |  |  | CI error-rate allowance (default: [ci].max_error_rate or 0) |
| `--n-boot` `N_BOOT` |  | `1000` | bootstrap resamples (default 1000) |
| `--test-ledger` `TEST_LEDGER` |  |  | test-split touch ledger (default: OUT/test_ledger.jsonl) |
| `--allow-test-retouch` |  |  | do not fail --ci when a candidate's test split was already reported |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. Without `--ci` this includes runs where a candidate regressed: read `report.json`, or pass `--ci` to turn the gate into the exit code. |
| `1` | `--ci` only: at least one candidate (or the reference) failed the gate. The reasons are printed per candidate and are in `--summary-md`. |
| `2` | Bad flags, config, golden set or `decide` function. Nothing ran. |
| `3` | Preflight rejected a candidate, or a provider SDK extra is missing. Nothing was spent on the golden set. |
| `4` | `budget_usd` / `--budget-usd` reached. Results so far are kept; re-run the same command (optionally with a higher budget) to resume. |

**Touches:** **Spends money** with a non-`fake` provider: one preflight request per candidate, then every golden row x `replies_per_row` (the reference `noise_replays` times). Reads the AWS price list over HTTPS (cached for a day; `CALIPER_PRICING_OFFLINE=1` stops it) and, for Bedrock models, the model's display name from Bedrock `GetFoundationModel` (free). With only `fake` candidates: offline and free.

## `caliper sample`

build a golden set from production logs

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--in` `IN` | yes |  | JSONL logs |
| `--out` `OUT` | yes |  | golden JSONL to write |
| `--stratum-field` `STRATUM_FIELD` | yes |  | dotted path, e.g. outcome |
| `--keep` `KEEP` | yes |  | strata kept whole, e.g. positive,unsure |
| `--negatives` `NEGATIVES` | yes |  | rows sampled from the rest |
| `--request-field` `REQUEST_FIELD` |  | `request` | dotted path to the request |
| `--id-field` `ID_FIELD` |  |  | dotted path to a row id |
| `--reference-field` `REFERENCE_FIELD` |  |  | dotted path -> reference_outcome |
| `--cap-per-stratum` `CAP_PER_STRATUM` |  |  | max rows per stratum |
| `--seed` `SEED` |  | `0` | sampling seed (default 0) |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files only.

## `caliper review`

list the outcomes a person must approve to measure which candidate is better

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--config` `CONFIG` | yes |  | eval TOML file |
| `--eval-out` `EVAL_OUT` | yes |  | the caliper eval --out folder to read |
| `--out` `OUT` | yes |  | folder for the review items (a new one) |
| `--candidates` `CANDIDATES` |  |  | comma-separated candidates (default: all but ref) |
| `--base-sample` `BASE_SAMPLE` |  | `30` | rows sampled at random to measure the reference's error rate (0 for none) |
| `--seed` `SEED` |  |  | sampling seed (default: config seed) |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files only, and free: reads the eval's `results.jsonl`, writes `items.jsonl` and, with the `label` extra, a `caliper.label.yaml` project. No model calls.

## `caliper example`

copy a bundled example project to try caliper

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `NAME` (optional) |  |  | example name (omit to list them) |
| `DIR` (optional) |  |  | destination (default: NAME) |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files only (copies a bundled project).

## `caliper kappa`

inter-rater agreement (--labels) or grader vs human labels (--rollouts)

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--labels` `LABELS` |  |  | LabelRecord JSONL (e.g. from caliper label) |
| `--rollouts` `ROLLOUTS` |  |  | labeled rollouts JSONL (metadata.human_label) |
| `--grader` `GRADER` |  |  | FILE.py:ATTR or module:attr (--rollouts) |
| `--by` `BY` |  |  | break down by category. One of: `category`. |
| `--source` `SOURCE` |  | `human` | label sources: human (default), all, or a comma list (human_end_user,internal_reviewer,llm_teacher,heuristic) |
| `--method` `METHOD` |  | `auto` | One of: `auto`, `cohen`, `fleiss`. |
| `--weighting` `WEIGHTING` |  | `quadratic` | grader-vs-label weighting (--rollouts). One of: `quadratic`, `linear`, `unweighted`. |
| `--bins` `BINS` |  |  | bin edges, e.g. 0,0.5,1 (--rollouts) |
| `--json` |  |  | print the report as JSON |

Exactly one of `--labels` / `--rollouts` is required.

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files only. `--grader` imports and runs your Python file.

## `caliper label`

Open the labelling UI: the start screen, or `PROJECT_DIR`. Needs the `label` extra (`pip install 'orlalabs-caliper[label]'`).

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `PROJECT_DIR` (optional) |  |  | a project folder |
| `--host` `HOST` |  | `127.0.0.1` | 0.0.0.0 to share on your network |
| `--port` `PORT` |  | `8765` |  |
| `--auth` `AUTH` |  | `none` | token: team mode, every request needs the access token (generated, or $CALIPER_LABEL_TOKEN). One of: `token`, `none`. |
| `--no-browser` |  |  | do not open a browser |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Starts a local web server and opens a browser. Reads the project's data source, which may be remote (S3, GCS, HTTP, Postgres, MySQL).

## `caliper label serve`

label a JSONL file with a label.yaml (legacy)

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `DATA` | yes |  | GoldenRow, EvalRow or plain JSONL rows |
| `--config` `CONFIG` | yes |  | label.yaml or label.toml |
| `--labeller` `LABELLER` |  |  | pre-fill the labeller name |
| `--store` `STORE` |  |  | labels.jsonl (default build/labels/&lt;data>.labels.jsonl) or labels.sqlite |
| `--host` `HOST` |  | `127.0.0.1` | 0.0.0.0 to share on your network |
| `--port` `PORT` |  | `8765` |  |
| `--auth` `AUTH` |  | `none` | token: team mode, every request needs the access token (generated, or $CALIPER_LABEL_TOKEN). One of: `token`, `none`. |
| `--no-browser` |  |  | do not open a browser |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Starts a local web server and opens a browser.

## `caliper label export`

write labels: golden, rollouts, records, csv, jsonl, parquet, a table

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `PROJECT_DIR` (optional) |  |  | project folder (default .) |
| `--format` `FORMAT` |  | `golden` | One of: `golden`, `rollouts`, `records`, `jsonl`, `csv`, `parquet`, `table`. |
| `--out` `OUT` |  |  | file path or s3://bucket/key |
| `--table` `TABLE` |  |  | write back to this table in the source database |
| `--overwrite` |  |  | s3: replace an existing object |
| `--rows` `ROWS` |  | `items` | label table: one row per item (final label) or one per label. One of: `items`, `labels`. |
| `--conflicts` `CONFLICTS` |  | `exclude` | double-labelled items that disagree (and are not adjudicated): leave out (default) or strict majority. One of: `exclude`, `majority`. |
| `--include-unlabelled` |  |  | golden: also write rows without a resolved label, unchanged; tables: also write conflicts and incomplete items, marked by status |
| `--data` `DATA` |  |  |  |
| `--config` `CONFIG` |  |  |  |
| `--store` `STORE` |  |  | default build/labels/&lt;data>.labels.jsonl |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files, or writes to S3 / the source database when `--out s3://...` or `--table` is given.

## `caliper label init`

create a project (caliper.label.yaml) from flags, without the browser

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `DIR` (optional) |  | `.` | project folder (default: here) |
| `--name` `NAME` |  |  | project name (default: from the folder name) |
| `--data` `DATA` |  |  | file, folder, glob, s3:// gs:// or https:// URL, .sqlite |
| `--kind` `KIND` |  |  | source kind (default: inferred from --data). One of: `file`, `folder`, `s3`, `gcs`, `http`, `postgres`, `mysql`, `sqlite`. |
| `--format` `FORMAT` |  |  | csv\|tsv\|jsonl\|json\|parquet\|xlsx (default: by extension) |
| `--table` `TABLE` |  |  | database table |
| `--query` `QUERY` |  |  | SQL query instead of --table (@file.sql reads a file) |
| `--id-column` `ID_COLUMN` |  |  | column holding a stable item id (default: row number) |
| `--env` `ENV` |  |  | env var with the DSN or token, e.g. DATABASE_URL |
| `--aws-profile` `AWS_PROFILE` |  |  |  |
| `--region` `REGION` |  |  |  |
| `--endpoint-url` `ENDPOINT_URL` |  |  | S3-compatible endpoint (R2, MinIO) |
| `--copy-data` |  |  | copy a local file into DIR/data/ first |
| `--no-detect` |  |  | do not open the source (needs --fields) |
| `--fields` `FIELDS` |  |  | columns to show, comma separated; `col:type` sets the display type (text, long_text, number, badge, link, image, json). Default: first 4 columns |
| `--labels` `LABELS` |  | `correct_wrong_unsure` | label template (default: correct_wrong_unsure). One of: `correct_wrong_unsure`, `true_uncertain_false`, `yes_no_unsure`, `categories`, `rating_1_5`, `free_text`, `score`. |
| `--categories` `CATEGORIES` |  |  | for --labels categories: 'A,B,C' or @file (one per line) |
| `--options` `OPTIONS` |  |  | your own options, comma separated (hotkeys 1..9) |
| `--approved` `APPROVED` |  |  | with --options: which ones count as approved |
| `--multi` |  |  | allow several options per item |
| `--queue-by` `QUEUE_BY` |  |  | split queues by this column (e.g. stratum) |
| `--double-label` `DOUBLE_LABEL` |  | `1` | labels per item (1-10) |
| `--assignment` `ASSIGNMENT` |  | `open` | One of: `open`, `round_robin`. |
| `--instructions` `INSTRUCTIONS` |  |  | markdown, or @file.md |
| `--golden-column` `GOLDEN_COLUMN` |  |  | column with known answers (hidden golden checks) |
| `--golden-rate` `GOLDEN_RATE` |  | `0.05` |  |
| `--from-legacy` `LABEL_YAML` |  |  | convert an old label.yaml (with --data) |
| `--snapshot` |  |  | also snapshot the items now |
| `--force` |  |  | overwrite an existing config |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Writes `caliper.label.yaml`. Opens the data source to detect columns unless `--no-detect`.

## `caliper-compile`

Compile a caliper @grader + EvalRow dataset into training-target artifacts.

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--target` `TARGET` | yes |  | One of: `trl`, `bedrock`, `openai`. |
| `--grader` `PATH.py[:ATTR]` | yes |  | Python file and the @grader-decorated attribute to use |
| `--dataset` `PATH.jsonl` | yes |  | EvalRow JSONL ({id, rollout: {prompt, response, gold, metadata}}) |
| `--out` `DIR` | yes |  | output directory |
| `--on-error` `SCORE` |  | `0.0` | score when the grader raises/returns an invalid score (default 0.0; 'none' = TRL skips the sample; trl target only) |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Bad flags or input (the message on stderr says which). |

**Touches:** Local files only. Imports and runs your grader file.

## `caliper-server`

Caliper dry-run backend (HTTP). Needs the `server` extra.

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--host` `HOST` |  | `127.0.0.1` |  |
| `--port` `PORT` |  | `8000` |  |
| `--reload` |  |  | restart on code changes |

**Exit codes**

| Exit | Meaning |
| --- | --- |
| `0` | Stopped cleanly. |
| `2` | Bad flags. |

**Touches:** Starts a local HTTP server.
