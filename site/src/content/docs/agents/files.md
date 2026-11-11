---
title: "Files and schemas"
description: "Every file Caliper reads or writes, field by field, with a JSON Schema for each."
editUrl: false
---

:::note
Generated from the code by `scripts/gen_agent_docs.py`; edit the code, then re-run it.
:::

| File | Schema | Read by | Written by |
| --- | --- | --- | --- |
| `caliper.toml` | [eval-config](/schemas/eval-config.schema.json) | `caliper eval` | you |
| `golden.jsonl` | [golden-row](/schemas/golden-row.schema.json) | `caliper eval`, `caliper label serve` | `caliper sample`, `caliper label export --format golden` |
| `OUT/results.jsonl` | [results-row](/schemas/results-row.schema.json) | `caliper eval` (resume) | `caliper eval` |
| `OUT/report.json` | [eval-report](/schemas/eval-report.schema.json) | you | `caliper eval` |
| `caliper.label.yaml` | [label-project](/schemas/label-project.schema.json) | `caliper label` | `caliper label init`, the setup wizard |
| label records JSONL | [label-record](/schemas/label-record.schema.json) | `caliper kappa --labels` | `caliper label export --format records` |
| rollouts JSONL | [eval-row](/schemas/eval-row.schema.json) | `caliper-compile`, `caliper kappa --rollouts` | you |

Validate a file before running it, for example with `check-jsonschema`:

```bash
pip install check-jsonschema
check-jsonschema --schemafile https://caliper.orlalabs.com/schemas/golden-row.schema.json golden.jsonl  # JSON
```

`check-jsonschema` reads JSON, YAML and TOML documents; for JSONL, validate each line (`caliper eval` does this anyway and names the bad line).

## `caliper.toml`

The eval config, TOML, passed to `caliper eval --config` (any file name). Relative paths (`golden`, `decide`, `fake_script`) resolve against the config file's folder.

Schema: [https://caliper.orlalabs.com/schemas/eval-config.schema.json](/schemas/eval-config.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `golden` | string | yes |  | golden JSONL, relative to the config file |
| `decide` | string or null |  | `null` | "file.py:func" turning a row's replies into a Decision |
| `reference` | string or null |  | `null` | defaults to the first candidate |
| `noise_replays` | integer |  | `2` | times the reference is replayed (noise floor) (>= 1) |
| `budget_usd` | number or null |  | `null` | stop before spending more than this (>= 0) |
| `seed` | integer |  | `0` | bootstrap and sampling seed |
| `candidates` | list of [Candidate](#candidate) | yes |  | [[candidates]]; the first one is the reference unless `reference` says otherwise. (items >= 1) |
| `pricing` | map of string to [Price](#price) |  |  | model id -> price override |
| `costs` | map of string to number |  |  | "from->to" -> cost of that change |
| `ci` | [CIConfig](#ciconfig) |  |  | [ci] gate settings |
| `calibration` | [CalibrationConfig](#calibrationconfig) |  |  | [calibration] |
| `operating_point` | [OperatingPointConfig](#operatingpointconfig) or null |  | `null` | [operating_point] |
| `cost_projection` | [CostProjectionConfig](#costprojectionconfig) or null |  | `null` | [cost_projection], for $/month |
| `slices` | list of [SliceConfig](#sliceconfig) |  |  | [[slices]] with their own rules |

### Candidate

What is being evaluated. The identity is the whole tuple, not the model.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | string | yes |  | unique within a run, e.g. "baseline", "haiku-nothink" |
| `provider` | `"fake"` or `"anthropic"` or `"bedrock"` or `"bedrock_converse"` or `"openai_compat"` | yes |  | fake is offline and free; the others call (and bill) a provider |
| `model` | string | yes |  | the provider's model id, e.g. a Bedrock inference profile id |
| `thinking` | `"off"` or `"on"` |  | `"off"` | extended/adaptive thinking on or off |
| `thinking_budget_tokens` | integer or null |  | `null` | thinking budget, where the provider takes one |
| `output_mode` | `"text"` or `"json_schema"` or `"tool"` |  | `"text"` | text, provider-native JSON schema, or a tool call |
| `output_schema` | object or null |  | `null` | JSON Schema for json_schema / tool output |
| `replies_per_row` | integer |  | `1` | >1 for vote loops (>= 1) |
| `params` | object |  |  | Extra request params (temperature etc.); openai_compat reads its key from the env var named by params.api_key_env (default OPENAI_API_KEY). |
| `base_url` | string or null |  | `null` | openai_compat endpoint, or an Anthropic API base URL |
| `region` | string or null |  | `null` | AWS region for bedrock / bedrock_converse |
| `fake_script` | string or null |  | `null` | fake provider: "path/to/file.py:func" producing replies |

### Price

USD per million tokens. Cache-aware.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `input` | number | yes |  | USD per million input tokens |
| `output` | number | yes |  | USD per million output tokens (reasoning tokens bill as output) |
| `cache_read` | number or null |  | `null` | defaults to input if None |
| `cache_write` | number or null |  | `null` | defaults to input if None |
| `source` | string or null |  | `null` | URL + date checked |

### CIConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `max_regression` | number |  | `0.0` | allowed cost-weighted regression per row beyond replay noise |
| `require` | `"non_inferiority"` or `"no_evidence_of_regression"` |  | `"non_inferiority"` | which CI bound the gate reads |
| `max_error_rate` | number or null |  | `null` | Share of results allowed to end with no decision; --max-error-rate overrides it. (>= 0, &lt;= 1) |
| `max_cost_ratio` | number or null |  | `null` | Fail a candidate whose mean cost per execution is above this multiple of the reference's (1.0 = "no more expensive"). A candidate or reference with replies priced at $0 fails too: an unknown price must not read as free. (> 0) |

### CalibrationConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `thresholds` | list of number or null |  | `null` | score thresholds to report precision/coverage at |
| `target_precision` | number or null |  | `null` | Recommend the lowest threshold reaching this precision; --target-precision overrides it. (>= 0, &lt;= 1) |

### OperatingPointConfig

Matched-operating-point comparison (``[operating_point]``). ``auto_outcomes`` are the outcomes that count as "handled automatically" (auto-published, expensive call skipped, ...). The reference's share of those on val is the coverage the candidate's score threshold is matched to.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `auto_outcomes` | list of string | yes |  | outcomes that mean "handled automatically" (items >= 1) |

### CostProjectionConfig

Production volume, to turn cost per row into dollars per month (``[cost_projection]``). ``calls_per_day`` counts feature calls, i.e. golden-row equivalents (a vote loop's 3 replies are one call).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `calls_per_day` | number | yes |  | production feature calls per day (> 0) |
| `days_per_month` | number |  | `30.0` | (> 0) |

### SliceConfig

A named hard slice with its own pass rule (``[[slices]]``). A row belongs to the slice when ``name`` is in ``GoldenRow.slices`` or when it matches ``where`` (``{"stratum": "x"}`` / ``{"metadata.key": value}``; a list value means "any of"). ``label_sources`` restricts which label sources a label-based rule reads (default: ``min_precision`` reads human sources only, ``must_not_lose`` reads every labelled row).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | string | yes |  | also matches rows whose `slices` list holds this name |
| `rule` | `"max_regression"` or `"must_not_lose"` or `"min_precision"` | yes |  | max_regression, must_not_lose or min_precision |
| `threshold` | number or null |  | `null` | required except for must_not_lose |
| `where` | object |  |  | {"stratum": ...} / {"metadata.k": ...} |
| `label_sources` | list of `"human_end_user"` or `"internal_reviewer"` or `"llm_teacher"` or `"heuristic"` or `"adjudicated"` or null |  | `null` | label sources a label-based rule reads |

## `golden.jsonl`

The golden set, one JSON object per line. Written by `caliper sample` and `caliper label export --format golden`; read by `caliper eval`. Only `id` and `request` are required; `request` is passed to every candidate as-is.

Schema: [https://caliper.orlalabs.com/schemas/golden-row.schema.json](/schemas/golden-row.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | string | yes |  | unique row id |
| `request` | object | yes |  | the request as the feature sent it (see the class docstring) |
| `reference_outcome` | string or null |  | `null` | outcome production recorded, if known |
| `label` | string or null |  | `null` | human-approved outcome, if labeled |
| `label_approved` | boolean or null |  | `null` | human approved the feature's output (calibration) |
| `stratum` | string or null |  | `null` | e.g. "positive" / "unsure" / "negative" from the sampler |
| `metadata` | object |  |  | free-form; slices can match on it |
| `label_source` | `"human_end_user"` or `"internal_reviewer"` or `"llm_teacher"` or `"heuristic"` or `"adjudicated"` or null |  | `null` | provenance of label/label_approved |
| `label_stage` | string or null |  | `null` | e.g. "triage", "final_review"; free-form |
| `split` | `"train"` or `"val"` or `"test"` or null |  | `null` | selection hygiene: thresholds picked on val, test touched once |
| `slices` | list of string |  |  | named hard slices this row belongs to |

## `OUT/results.jsonl`

One line per (candidate, golden row, replica), appended while `caliper eval` runs. Re-running the same command resumes from it; `--no-resume` starts it fresh.

Schema: [https://caliper.orlalabs.com/schemas/results-row.schema.json](/schemas/results-row.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `run_id` | string | yes |  | the invocation that wrote this line |
| `candidate` | string | yes |  | candidate name from the config |
| `row_id` | string | yes |  | GoldenRow.id |
| `replica` | integer |  | `0` | replay index; the reference is replayed >= 2x for the noise floor |
| `request_sha256` | string | yes |  | hash of GoldenRow.request (pre-overlay) |
| `candidate_sha256` | string or null |  | `null` | hash of the Candidate settings (stale-resume check) |
| `replies` | list of [Reply](#reply) | yes |  | replies_per_row replies, in order |
| `decision` | [Decision](#decision) or null |  | `null` | None if decide() raised |
| `error` | string or null |  | `null` | decide() error or row-level failure |

### Reply

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `text` | string or null |  | `null` | the reply text, when there is one |
| `parsed` | any |  | `null` | structured output, when output_mode != text |
| `served_by` | [ServedBy](#servedby) or null |  | `null` | who answered, from the response |
| `usage` | [Usage](#usage) |  |  | token counts from the response |
| `latency_ms` | number |  | `0.0` | wall time of the request |
| `cost_usd` | number |  | `0.0` | usage x price; 0.0 when no price was found (see report warnings) |
| `error` | string or null |  | `null` | provider/transport/parse error; text/parsed may be None |

### Decision

The system outcome for one row, as the real feature would have decided.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `outcome` | string | yes |  | categorical outcome; transitions are computed on this |
| `score` | number or null |  | `null` | confidence score in [0, 1] for calibration, if any |
| `detail` | object |  |  | free-form, from decide() |

### ServedBy

Who actually answered. Never inferred from the request.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `provider` | `"fake"` or `"anthropic"` or `"bedrock"` or `"bedrock_converse"` or `"openai_compat"` | yes |  | provider that answered |
| `model` | string | yes |  | as reported by the response, not as requested |
| `endpoint` | string or null |  | `null` | base URL / region / "fake" |
| `matches_request` | boolean | yes |  | False => fallback/substitution happened; surfaced in reports |

### Usage

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `input_tokens` | integer |  | `0` | uncached input tokens |
| `output_tokens` | integer |  | `0` | output tokens, reasoning included |
| `cache_read_tokens` | integer |  | `0` | input tokens read from the prompt cache |
| `cache_write_tokens` | integer |  | `0` | input tokens written to the prompt cache |

## `OUT/report.json`

Everything `caliper eval` prints, and more, as JSON. Parse this instead of stdout. The gate verdict itself is not in it: use the exit code of `--ci`, or `--summary-md`.

Schema: [https://caliper.orlalabs.com/schemas/eval-report.schema.json](/schemas/eval-report.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `reference` | string | yes |  | reference candidate name |
| `seed` | integer | yes |  | [seed] from the config |
| `n_boot` | integer | yes |  | bootstrap resamples behind every CI |
| `n_golden` | integer | yes |  | rows in the golden set |
| `noise_replays` | integer | yes |  | times the reference was replayed |
| `max_regression` | number | yes |  | [ci] max_regression |
| `require` | string |  | `"non_inferiority"` | [ci] require |
| `max_cost_ratio` | number or null |  | `null` | [ci] max_cost_ratio |
| `calls_per_day` | number or null |  | `null` | [cost_projection] calls_per_day |
| `days_per_month` | number or null |  | `null` | [cost_projection] days_per_month |
| `noise_floor` | [NoiseFloor](#noisefloor) or null | yes |  | the reference's own replay-to-replay variation |
| `provenance` | [ProvenanceSummary](#provenancesummary) or null |  | `null` | label sources and splits in the golden set |
| `slices` | list of [SliceDef](#slicedef) |  |  | [[slices]] and their row counts |
| `candidates` | list of [CandidateReport](#candidatereport) | yes |  | one per candidate, the reference included |
| `warnings` | list of string |  |  | e.g. approximate or missing prices |

### NoiseFloor

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `reference` | string | yes |  | reference candidate name |
| `n_replicas` | integer | yes |  | times the reference was replayed |
| `n_rows` | integer | yes |  | rows with every replica decided (denominator) |
| `n_errored_rows` | integer | yes |  | rows where a replica has no decision |
| `n_incomplete_rows` | integer | yes |  | fewer replicas than n_replicas recorded |
| `n_flipped_rows` | integer | yes |  | rows whose outcome differs between the reference's own replays |
| `flip_rate` | [CI](#ci) | yes |  | share of rows that flipped between replays |
| `cost_weighted_flip_rate` | [CI](#ci) | yes |  | the same, weighted by [costs]: the noise the gate subtracts |
| `flipped_rows` | list of string |  |  | ids of flipped rows |
| `errored_rows` | list of string |  |  | ids of errored rows |

### ProvenanceSummary

Where the golden set's labels come from, and how it is split.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `n_labelled` | integer | yes |  | rows with label_approved |
| `by_source` | map of string to integer |  |  | labelled rows per source key |
| `by_split` | map of string to integer |  |  | all rows per split ("none") |
| `n_human_truth` | integer |  | `0` |  |
| `has_human_truth` | boolean |  | `false` |  |
| `has_splits` | boolean |  | `false` |  |

### SliceDef

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | string | yes |  |  |
| `rule` | string | yes |  |  |
| `threshold` | number or null | yes |  |  |
| `n_rows` | integer | yes |  | golden rows in the slice |
| `n_report_rows` | integer | yes |  | of those, on the report split |

### CandidateReport

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | string | yes |  | candidate name |
| `provider` | string | yes |  | provider kind |
| `model` | string | yes |  | requested model id |
| `thinking` | string | yes |  | thinking mode |
| `output_mode` | string | yes |  | output mode |
| `is_reference` | boolean | yes |  | true for the reference candidate |
| `rows` | integer | yes |  | distinct golden rows with a result |
| `results` | integer | yes |  | (row, replica) executions |
| `replies` | integer | yes |  |  |
| `provider_errors` | integer | yes |  | replies with an error |
| `decide_errors` | integer | yes |  | results with no decision |
| `error_rate` | number | yes |  | decide_errors / results |
| `tokens` | [Tokens](#tokens) | yes |  | summed token counts |
| `latency_p50_ms` | number | yes |  | median reply latency |
| `latency_p95_ms` | number | yes |  | 95th percentile reply latency |
| `dollars` | number | yes |  | total spend for this candidate in this report |
| `cost_per_row` | number or null |  | `null` | dollars / results |
| `cost_vs_reference` | number or null |  | `null` | cost_per_row / reference cost_per_row |
| `unpriced_replies` | integer |  | `0` | non-error replies that used tokens but cost $0 |
| `monthly_cost_usd` | number or null |  | `null` | with [cost_projection] |
| `monthly_delta_usd` | number or null |  | `null` | vs the reference, with [cost_projection] |
| `served_models` | list of string |  |  | "provider:model", sorted |
| `served_mismatches` | integer |  | `0` | replies with matches_request=False |
| `served_unknown` | integer |  | `0` | non-error replies with no served_by |
| `stale_results` | integer |  | `0` | recorded for a different request or candidate settings than now |
| `transitions` | [Transitions](#transitions) or null |  | `null` | outcome changes vs the reference |
| `paired_excess` | [PairedExcess](#pairedexcess) or null |  | `null` | regression - replay noise, paired per row |
| `calibration` | [Calibration](#calibration) or null |  | `null` | precision/coverage by score threshold |
| `matched_operating_point` | [MatchedOperatingPoint](#matchedoperatingpoint) or null |  | `null` |  |
| `slices` | list of [SliceResult](#sliceresult) |  |  | per-slice pass/fail |

### CI

Percentile bootstrap confidence interval around a point estimate.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `estimate` | number | yes |  | point estimate |
| `low` | number | yes |  | lower bound of the bootstrap interval |
| `high` | number | yes |  | upper bound of the bootstrap interval |
| `confidence` | number |  | `0.95` | interval level, e.g. 0.95 |
| `n_boot` | integer |  | `1000` | bootstrap resamples |

### Tokens

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `input` | integer |  | `0` |  |
| `output` | integer |  | `0` |  |
| `cache_read` | integer |  | `0` |  |
| `cache_write` | integer |  | `0` |  |

### Transitions

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `candidate` | string | yes |  | candidate name |
| `reference` | string | yes |  | reference name |
| `n_rows` | integer | yes |  | compared rows (both sides decided) |
| `n_errored` | integer | yes |  | either side has no decision |
| `n_missing` | integer | yes |  | row present on one side only |
| `matrix` | list of [TransitionCount](#transitioncount) |  |  | reference -> candidate counts |
| `flipped` | list of [FlippedRow](#flippedrow) |  |  | rows whose outcome changed |
| `errored_rows` | list of string |  |  | ids with no decision on a side |
| `flip_rate` | [CI](#ci) | yes |  | share of compared rows that changed outcome |
| `regression` | [CI](#ci) | yes |  | cost-weighted |

### PairedExcess

Candidate regression minus reference replay noise, paired per row. For every compared row ``i``: ``d_i = cost(ref replica 0 -> candidate) - n_i`` where ``n_i`` is the reference's own symmetrized replay cost on that row (0 when the row has no complete noise replays). ``delta`` is the paired bootstrap CI of ``mean(d)``: one row resample per draw, both terms computed on the same rows.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `n_rows` | integer | yes |  | compared rows |
| `n_without_noise` | integer | yes |  | compared rows with no complete reference replays (n_i = 0) |
| `regression` | number | yes |  | mean candidate cost on these rows |
| `noise` | number | yes |  | mean reference replay cost on these rows |
| `delta` | [CI](#ci) | yes |  | paired: regression - noise |

### Calibration

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `candidate` | string | yes |  |  |
| `basis` | string | yes |  | kind of the labels the sweep reads (see provenance.basis_kind) |
| `metric` | string | yes |  | "precision" for human truth, else e.g. "teacher agreement" |
| `basis_sources` | list of string |  |  |  |
| `selection_split` | string |  | `"all"` | "val" when the golden set is split |
| `report_split` | string |  | `"all"` | "test" when the golden set is split |
| `n_rows` | integer | yes |  | selection rows (labelled, usable score, in basis) |
| `n_approved` | integer | yes |  |  |
| `n_unlabeled` | integer | yes |  |  |
| `n_no_score` | integer | yes |  | labeled, but no decision or no score (errored / unscored) |
| `n_out_of_range` | integer | yes |  | score outside [0, 1]; excluded, never clamped |
| `sweep` | list of [ThresholdPoint](#thresholdpoint) |  |  |  |
| `reliability` | list of [ReliabilityBin](#reliabilitybin) |  |  |  |
| `ece` | number or null |  | `null` |  |
| `target_precision` | number or null |  | `null` |  |
| `recommended_threshold` | number or null |  | `null` |  |
| `on_report` | list of [SourceMetrics](#sourcemetrics) |  |  | per source, at recommended |
| `sweep_by_source` | map of string to list of [ThresholdPoint](#thresholdpoint) |  |  | The sweep on the selection rows of each label source separately (JSON only). |

### MatchedOperatingPoint

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `candidate` | string | yes |  |  |
| `reference` | string | yes |  |  |
| `auto_outcomes` | list of string | yes |  |  |
| `selection_split` | string | yes |  |  |
| `report_split` | string | yes |  |  |
| `n_selection` | integer | yes |  | selection rows with both sides decided |
| `reference_coverage` | number | yes |  | on selection rows |
| `threshold` | number or null | yes |  | candidate score threshold; None = its own outcomes |
| `candidate_coverage` | number | yes |  | on selection rows, at threshold |
| `n_report` | integer | yes |  | labelled report rows with both sides decided |
| `n_excluded` | integer | yes |  | report rows dropped: a side has no decision |
| `reference_coverage_report` | number | yes |  |  |
| `candidate_coverage_report` | number | yes |  |  |
| `by_source` | list of [SourceLosses](#sourcelosses) |  |  |  |

### SliceResult

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `slice` | string | yes |  |  |
| `rule` | string | yes |  |  |
| `candidate` | string | yes |  |  |
| `threshold` | number or null | yes |  |  |
| `n_rows` | integer | yes |  | rows the rule read |
| `metric` | string | yes |  | what ``value`` is |
| `value` | number or null | yes |  |  |
| `passed` | boolean | yes |  |  |
| `detail` | string | yes |  | one line, for the report and the gate |
| `rows` | list of string |  |  | offending rows (lost, flipped, ...) |
| `paired` | [PairedExcess](#pairedexcess) or null |  | `null` |  |
| `reference_lost` | integer or null |  | `null` | must_not_lose: slice rows each side gets wrong |
| `candidate_lost` | integer or null |  | `null` |  |

### TransitionCount

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `src` | string | yes |  |  |
| `dst` | string | yes |  |  |
| `count` | integer | yes |  |  |
| `unit_cost` | number | yes |  |  |

### FlippedRow

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `row_id` | string | yes |  |  |
| `src` | string | yes |  |  |
| `dst` | string | yes |  |  |
| `cost` | number | yes |  |  |

### ThresholdPoint

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `threshold` | number | yes |  |  |
| `n_auto` | integer | yes |  |  |
| `coverage` | number | yes |  |  |
| `auto_approved_rate` | number or null | yes |  | named by Calibration.metric (precision only for humans) |
| `approved_to_review` | number or null | yes |  |  |
| `kappa` | number or null | yes |  |  |

### ReliabilityBin

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `low` | number | yes |  |  |
| `high` | number | yes |  |  |
| `n` | integer | yes |  |  |
| `mean_score` | number or null | yes |  |  |
| `approved_rate` | number or null | yes |  |  |

### SourceMetrics

One label source's view of a threshold on the report rows.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `source` | string | yes |  | LabelSource value or "unknown" |
| `kind` | string | yes |  | human_truth \| teacher_agreement \| heuristic_agreement \| unknown_source |
| `metric` | string | yes |  | what auto_approved_rate is called for this source |
| `n_rows` | integer | yes |  | labelled report rows from this source with a usable score |
| `n_approved` | integer | yes |  |  |
| `threshold` | number or null | yes |  |  |
| `n_auto` | integer |  | `0` |  |
| `coverage` | number or null |  | `null` |  |
| `auto_approved_rate` | number or null |  | `null` |  |
| `approved_to_review` | number or null |  | `null` |  |
| `kappa` | number or null |  | `null` |  |
| `ece` | number or null |  | `null` |  |

### SourceLosses

Losses on the report rows of one label source.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `source` | string | yes |  |  |
| `kind` | string | yes |  | human_truth for human sources; agreement kinds otherwise |
| `metric` | string | yes |  | "precision" (truth) or e.g. "teacher agreement": names what a loss is |
| `n_rows` | integer | yes |  |  |
| `reference` | [SideLosses](#sidelosses) | yes |  |  |
| `candidate` | [SideLosses](#sidelosses) | yes |  |  |
| `candidate_lost` | list of string |  |  | every row the candidate loses |
| `reference_lost` | list of string |  |  | every row the reference loses |
| `candidate_only` | list of string |  |  | ref right, candidate wrong |
| `reference_only` | list of string |  |  | candidate right, ref wrong |
| `delta` | [CI](#ci) | yes |  | paired: mean(candidate lost) - mean(reference lost) per row |

### SideLosses

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `n_auto` | integer | yes |  |  |
| `missed` | integer | yes |  | approved, not auto-handled |
| `bad_auto` | integer | yes |  | not approved, auto-handled |

## `caliper.label.yaml`

A label project, YAML 1.2. Written by `caliper label init` or the setup wizard. Credentials are never stored here: `credentials.env` names an environment variable.

Schema: [https://caliper.orlalabs.com/schemas/label-project.schema.json](/schemas/label-project.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `version` | integer |  | `1` | schema version of this file (always 1 for now) |
| `revision` | integer |  | `1` | config revision: bumped by every settings save (>= 1) |
| `name` | string |  | `"Untitled project"` |  |
| `description` | string or null |  | `null` |  |
| `source` | [SourceConfig](#sourceconfig) | yes |  |  |
| `card` | [CardConfig](#cardconfig) |  |  |  |
| `labels` | [LabelsConfig](#labelsconfig) |  |  |  |
| `workflow` | [WorkflowConfig](#workflowconfig) |  |  |  |
| `exports` | list of [ExportTarget](#exporttarget) |  |  |  |
| `rollout` | [RolloutConfig](#rolloutconfig) or null |  | `null` |  |

### SourceConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | `"file"` or `"folder"` or `"s3"` or `"gcs"` or `"http"` or `"postgres"` or `"mysql"` or `"sqlite"` or `"plugin"` | yes |  |  |
| `path` | string or null |  | `null` | file/folder/sqlite path, or URL / glob for s3/gcs/http |
| `format` | string or null |  | `null` | csv\|tsv\|jsonl\|json\|parquet\|xlsx; None = by extension |
| `table` | string or null |  | `null` | databases: a table ... |
| `query` | string or null |  | `null` | ... or a SQL query (exactly one of table/query) |
| `sidecar` | string or null |  | `null` | folder sources: CSV joined on file name |
| `id_column` | string or null |  | `null` | None = row number (stable within a snapshot) |
| `plugin` | string or null |  | `null` | kind=plugin: entry-point name |
| `options` | object |  |  | connector-specific, non-secret |
| `credentials` | [Credentials](#credentials) |  |  |  |

### CardConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `fields` | list of [CardField](#cardfield) |  |  |  |

### LabelsConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | `"single"` or `"multi"` or `"yes_no_unsure"` or `"free_text"` or `"score"` |  | `"single"` |  |
| `options` | list of [LabelOption](#labeloption) |  |  |  |
| `score` | [ScoreRange](#scorerange) or null |  | `null` | type=score |
| `keys` | [Keys](#keys) |  |  |  |
| `category_column` | string or null |  | `null` | inter-annotator kappa by category |

### WorkflowConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `queues` | [QueueConfig](#queueconfig) |  |  |  |
| `double_label` | integer |  | `1` | (>= 1, &lt;= 10) |
| `assignment` | `"open"` or `"round_robin"` |  | `"open"` |  |
| `instructions` | string or null |  | `null` | markdown |
| `golden` | [GoldenCheck](#goldencheck) or null |  | `null` |  |
| `source` | `"human_end_user"` or `"internal_reviewer"` or `"llm_teacher"` or `"heuristic"` or `"adjudicated"` |  | `"internal_reviewer"` |  |
| `stage` | string or null |  | `null` |  |

### ExportTarget

A saved export destination (optional). Same connector/credential rules as source.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | string | yes |  |  |
| `format` | string | yes |  | golden\|rollouts\|records\|csv\|jsonl\|parquet\|table |
| `destination` | [SourceConfig](#sourceconfig) | yes |  | file path / s3 URL / database table |

### RolloutConfig

`export --format rollouts` for non-EvalRow rows: dotted paths (legacy `rollout:`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `prompt` | string or null |  | `null` |  |
| `response` | string or null |  | `null` |  |
| `gold` | string or null |  | `null` |  |

### Credentials

References only. Never a secret value.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `env` | string or null |  | `null` | e.g. "DATABASE_URL" (whole DSN) or "PGPASSWORD" |
| `env_map` | map of string to string |  |  | option -> ENV_VAR |
| `aws_profile` | string or null |  | `null` | else default chain (env, SSO, instance role) |
| `region` | string or null |  | `null` |  |
| `endpoint_url` | string or null |  | `null` | S3-compatible endpoints (not a secret) |

### CardField

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `column` | string or null |  | `null` | source column; dotted paths into JSON columns allowed |
| `template` | string or null |  | `null` | "cards/{id}.svg" style; exactly one of column/template |
| `type` | `"text"` or `"long_text"` or `"number"` or `"badge"` or `"link"` or `"image"` or `"json"` or `"datetime"` |  | `"text"` |  |
| `label` | string or null |  | `null` |  |
| `decimals` | integer or null |  | `null` | (>= 0, &lt;= 10) |
| `hidden` | boolean |  | `false` | kept in snapshot/export but not shown |
| `mono` | boolean |  | `false` | text: monospace, keep whitespace (legacy FieldSpec.mono) |

### LabelOption

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `value` | string | yes |  | stored value; stable identity (renames change `label`, or migrate) |
| `label` | string or null |  | `null` |  |
| `hotkey` | string or null |  | `null` | auto-assigned 1..9 by the wizard; clash-checked |
| `color` | string or null |  | `null` | "#rrggbb" |
| `approved` | boolean |  | `false` | counts as approved/positive (label_approved, human_label=1) |
| `requires_note` | boolean |  | `false` |  |
| `outcome` | string or null |  | `null` | GoldenRow.label on export; default: value |
| `score` | number or null |  | `null` | human_label override (>= 0.0, &lt;= 1.0) |
| `retired` | boolean |  | `false` | removed in settings after labels existed: kept, flagged |

### ScoreRange

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `min` | number |  | `1` |  |
| `max` | number |  | `5` |  |
| `step` | number |  | `1` |  |

### Keys

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `skip` | string |  | `"s"` |  |
| `undo` | string |  | `"backspace"` |  |
| `note` | string |  | `"n"` |  |

### QueueConfig

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `by` | string or null |  | `null` | column (or "stratum" from `caliper sample`) |

### GoldenCheck

Hidden items with known answers; per-labeller accuracy on them.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `answer_column` | string | yes |  | rows with a non-empty value here are golden items |
| `rate` | number |  | `0.05` | share of served items (>= 0.0, &lt;= 1.0) |

## `label records JSONL`

One label event per line: `caliper label export --format records` writes it and `caliper kappa --labels` reads it.

Schema: [https://caliper.orlalabs.com/schemas/label-record.schema.json](/schemas/label-record.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `item_id` | string | yes |  |  |
| `decision` | string or null | yes |  | a configured decision key's value; None when skipped |
| `skipped` | boolean |  | `false` |  |
| `labeller` | string | yes |  |  |
| `source` | `"human_end_user"` or `"internal_reviewer"` or `"llm_teacher"` or `"heuristic"` or `"adjudicated"` |  | `"internal_reviewer"` |  |
| `stage` | string or null |  | `null` |  |
| `labelled_at` | string | yes |  | ISO-8601 UTC, e.g. "2026-11-06T12:00:00Z" |
| `queue` | string or null |  | `null` |  |
| `category` | string or null |  | `null` | for inter-rater kappa by category |
| `note` | string or null |  | `null` |  |
| `undoes` | string or null |  | `null` | record_id this record retracts (undo), if any |
| `record_id` | string | yes |  | stable unique id (e.g. sha256 of the other fields), assigned by the writer |

## `rollouts JSONL`

Graded rollouts for `caliper-compile --dataset` and `caliper kappa --rollouts` (`rollout.metadata.human_label` holds the human label for kappa).

Schema: [https://caliper.orlalabs.com/schemas/eval-row.schema.json](/schemas/eval-row.schema.json)

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | string | yes |  | unique row id |
| `rollout` | [Rollout](#rollout) | yes |  | the rollout to grade |

### Rollout

A single model rollout to be graded. `gold` is optional because some graders are reference-free (LLM-as-judge, rubric-based). `trace` carries tool-call records for agent grading.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `prompt` | string | yes |  | what the model was asked |
| `response` | string | yes |  | what it answered |
| `gold` | string or null |  | `null` | reference answer, for graders that use one |
| `trace` | list of object |  |  | tool-call records |
| `metadata` | object |  |  | free-form; human_label for kappa |
