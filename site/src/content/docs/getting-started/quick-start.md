---
title: Quick Start
description: Copy the bundled example and run an eval, offline and without API keys.
---

The bundled example is a fictional marketplace feature. An LLM fills product-catalog records (description, specs, a tax-category code) with a confidence score, and low scores go to a person. Everything runs on a deterministic fake provider: no network, no keys.

```bash
pip install orlalabs-caliper

# Copy the example project into ./catalog_cleanup
caliper example catalog_cleanup

# Run the eval. --ci exits 1 here on purpose: two candidates fail the gate.
caliper eval --config catalog_cleanup/caliper.toml --out catalog_cleanup/out \
    --ci --summary-md catalog_cleanup/out/summary.md
```

`catalog_cleanup/out/summary.md` is the comment Caliper would post on the pull request.

## What you'll see

- The baseline's noise floor is 1 flip in 80 rows.
- A cheaper model sends 19 auto-published records to review, mostly rare product families. Its cost-weighted regression is 0.300, which is 0.263 [0.163, 0.362] above the baseline's own noise (paired), so the gate fails.
- The same cheap model with thinking on is non-inferior overall (paired excess 0.013 [-0.087, 0.087]), but it is not cheaper: its thinking tokens bring it to 1.04x the baseline's cost per row (+$87/month at 20,000 calls a day). CI still blocks it, because on test it loses one record in the `rare_code` must-not-lose slice that the current model handles.
- Calibration picks the auto-publish threshold on the validation split: 0.70, at 95.2% precision on 28 reviewer-labelled rows. On test the same threshold gives 85.7% precision on reviewer labels, plus 75.0% *teacher agreement* on the LLM-labelled rows, reported separately.

Every number comes from the simulation, not from a real system. The [worked example](/examples/catalog-cleanup/) explains each step.

## Build a golden set from logs

```bash
caliper sample --in catalog_cleanup/logs.jsonl --out catalog_cleanup/out/sampled.jsonl \
    --stratum-field outcome --keep needs_review,fallback --negatives 40 --id-field id
```

Rare outcomes (`needs_review`, `fallback`) are kept whole; 40 rows are sampled from the rest.

## Use it on your own feature

Write three files:

1. `golden.jsonl`: rows holding the exact requests your feature sent.
2. `decide.py`: your feature's routing logic.
3. `caliper.toml`: the candidates, costs and gate.

The [caliper eval guide](/eval/guide/) has the concepts, the config reference and the CLI reference. To run real models, install a provider extra (see [Installation](/getting-started/installation/)); `catalog_cleanup/caliper.real.toml` runs the same golden set on Claude models.
