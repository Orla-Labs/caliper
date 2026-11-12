---
title: "Caliper for agents"
description: "Everything a coding agent needs to use Caliper correctly: what it is for, a free smoke test, the rules that prevent wrong conclusions, and where the machine-readable reference lives."
---

This section is written for AI coding agents and the people who run them. Each page is
also served as plain Markdown: add `.md` to its URL (this page is
[/agents.md](/agents.md)). The whole site is at [/llms.txt](/llms.txt) (an index) and
[/llms-full.txt](/llms-full.txt) (every page in one file).

## What Caliper does

Caliper answers one question: **if we change the model, prompt or settings behind an AI
feature, does the feature's outcome get worse, and what does it cost?** It replays a
*golden set* of real requests through each *candidate*, turns each candidate's replies
into the feature's own decision with your `decide()` function, and compares those
decisions with the *reference* (what production runs today). Changes are measured
against the reference's own replay-to-replay noise, weighted by what each kind of change
costs you, and reported with bootstrap confidence intervals, cost per call and a monthly
projection. `--ci` turns the verdict into an exit code for a pull request.

Use Caliper when:

- someone wants to switch a feature to another model, provider, thinking setting or
  output mode, and you need evidence it is safe and what it saves;
- a pull request changes a prompt or model and should be gated on regression and cost;
- you need a golden set from production logs (`caliper sample`) or human labels
  (`caliper label`);
- you are building a reward grader for RL fine-tuning and want it mutation-tested,
  checked against human labels (`caliper kappa`) and compiled (`caliper-compile`).

Don't use Caliper for:

- generic benchmark scores (MMLU-style); it measures *your* feature's decisions;
- judging free text with no decision behind it; Caliper compares categorical outcomes;
- production monitoring or tracing; it runs offline against files and keeps no server.

## Install

```bash
pip install orlalabs-caliper          # or: uv add orlalabs-caliper / uvx --from orlalabs-caliper caliper
```

The core and the offline `fake` provider need nothing else. Providers and tools are extras:

| Extra | Adds |
| --- | --- |
| `anthropic` | `provider = "anthropic"` (Anthropic API) |
| `bedrock` | `provider = "bedrock"` (Claude on Amazon Bedrock) |
| `bedrock-converse` | `provider = "bedrock_converse"` (any Bedrock model via Converse) |
| `label` | `caliper label` (local labelling UI and data connectors) |
| `server` | `caliper-server` |
| `all` | everything above, plus `postgres` |

`provider = "openai_compat"` needs no extra. Install extras with
`pip install 'orlalabs-caliper[anthropic,label]'`. A missing extra makes `caliper eval`
exit `3` with the exact `pip install` line to run.

## Smoke test (offline, free, a few seconds)

Run this before anything else; it proves the install works and shows you real output:

```bash
caliper example catalog_cleanup demo
caliper eval --config demo/caliper.toml --out demo/out
echo $?    # 0
jq -c '.candidates[] | {name, cost_per_row, cost_vs_reference, monthly_cost_usd}' demo/out/report.json
```

```json
{"name":"baseline","cost_per_row":0.0037923562500000057,"cost_vs_reference":1.0,"monthly_cost_usd":2275.4137500000033}
{"name":"small","cost_per_row":0.0009982199999999993,"cost_vs_reference":0.26321894204954976,"monthly_cost_usd":598.9319999999996}
{"name":"small-thinking","cost_per_row":0.003936919999999999,"cost_vs_reference":1.0381197705252487,"monthly_cost_usd":2362.1519999999996}
```

With the gate on, the same example **fails on purpose** (the cheaper model regresses and
loses rare rows), so exit `1` is the expected result here:

```bash
caliper eval --config demo/caliper.toml --out demo/out-ci --ci --summary-md demo/out-ci/summary.md
echo $?    # 1
```

## How a run works

```
golden.jsonl ──► each candidate ──► replies ──► decide(replies) ──► outcome per row
                (reference replayed noise_replays times)                │
                                                                        ▼
        report.json ◄── gate (--ci) ◄── transitions vs reference − replay noise, cost
```

- A **candidate** is a whole configuration (provider, model, thinking, output mode,
  params, replies per row), not just a model id.
- `decide(replies) -> Decision` is your feature's logic, copied from production.
  Caliper compares `Decision.outcome`, never raw text.
- `[costs]` weights each kind of change (`"auto_publish->needs_review" = 1.0`); an
  unlisted change costs `1.0`.

## Rules that prevent wrong conclusions

1. **Exit `0` without `--ci` is not a pass.** It only means the run finished. Pass `--ci`
   to get a verdict as the exit code, or read `report.json`.
2. **Use a fresh `--out` per question.** Every run that reports on the test split records
   it in `OUT/test_ledger.jsonl`. A later `--ci` run into the same `--out` fails with
   `test split re-touched`, which is a selection-hygiene rule, not a regression. A reused
   `--out` also resumes old results; `--no-resume` starts over.
3. **Parse `OUT/report.json`, not stdout.** Its schema is
   [/schemas/eval-report.schema.json](/schemas/eval-report.schema.json). The gate
   verdict is the exit code of `--ci` (reasons in stdout and `--summary-md`).
4. **Ask before spending money.** Any candidate whose provider is not `fake` makes paid
   API calls. Get your user's go-ahead, set `budget_usd`, and read
   [What costs money](/agents/spending/).
5. **`$0` means unknown, not free.** A model with no price is billed at `$0`, shown as
   `unpriced`, and warned about. Add `[pricing."<model-id>"]` (USD per million tokens).
   With `[ci] max_cost_ratio` an unpriced candidate fails the gate.
6. **Exit `3` is not always a bad request shape.** It is also how a missing provider
   extra shows up; read the message.
7. **Paths in a config resolve against the config file's folder**, not the working
   directory.
8. **After editing `decide.py` or a fake script, pass `--no-resume`.** Resume detects
   changed requests and candidate settings, not changed code.
9. **Approvals come from people, never from you.** `[quality]` approvals are human
   verdicts. Do not write them yourself or with a model: non-human sources are ignored,
   and labelling model output as `internal_reviewer` defeats the gate. Run
   `caliper review`, then hand the labelling to your user
   ([recipe 10](/agents/recipes/#10-is-the-more-expensive-model-worth-it)).

## Where to go next

| Page | For |
| --- | --- |
| [Recipes](/agents/recipes/) | Step-by-step tasks, each with a command that verifies it worked |
| [What costs money](/agents/spending/) | Which commands bill, estimating cost, credentials |
| [Errors and environment](/agents/troubleshooting/) | Exact error text and the fix; every environment variable |
| [CLI reference](/agents/cli/) | Every command and flag, exit codes, what it touches |
| [Files and schemas](/agents/files/) | Every file format, field by field, with JSON Schemas |
| [Instructions for your agent](/agents/instructions/) | A drop-in `AGENTS.md` section and a Claude Code skill |

The concept guide behind all of this is [caliper eval](/eval/guide/).
