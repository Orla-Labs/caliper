---
name: caliper
description: Evaluate whether a change to an AI feature's model, prompt, provider or settings makes its outcomes worse or more expensive, using Caliper (orlalabs-caliper). Use when asked to compare models, switch to a cheaper model, check a prompt change, gate a pull request on LLM regressions or cost, build a golden set from logs, or project LLM cost per month.
---

# Caliper

Caliper replays a golden set of real requests through candidate configurations, applies the
feature's own `decide()` logic to the replies, and compares the decisions with the reference
(what production runs today), net of the reference's own replay noise, with cost per row and
a monthly projection.

Full agent docs: https://caliper.orlalabs.com/agents/ . Fetch any page as Markdown by adding
`.md` (for example https://caliper.orlalabs.com/agents/recipes.md), or everything at once
from https://caliper.orlalabs.com/llms-full.txt. Read the recipe for the task before writing
config.

## Steps

1. Check the install: `caliper --help`. If missing: `pip install orlalabs-caliper` plus the
   extras the providers need (`anthropic`, `bedrock`, `bedrock-converse`; `label` for the
   labelling UI). Python 3.12+.
2. New to this repo's setup? Run the free offline smoke test first:
   `caliper example catalog_cleanup demo && caliper eval --config demo/caliper.toml --out demo/out`.
3. Find or write the three inputs: `caliper.toml` (candidates, reference, `[costs]`, `[ci]`,
   `budget_usd`), `golden.jsonl` (`id` + `request` per line), `decide.py`
   (`decide(replies: list[Reply]) -> Decision`). Schemas:
   https://caliper.orlalabs.com/agents/files.md
4. Before any run with a non-`fake` provider: estimate the cost (pilot on 10-20 rows, then
   `cost_per_row x rows x executions`), tell the user, and wait for their go-ahead.
5. Run with a fresh output folder:
   `caliper eval --config caliper.toml --out build/caliper-<timestamp> --ci --summary-md build/summary.md`.
6. Report from `OUT/report.json`: per candidate `paired_excess.delta` (regression beyond
   noise, with CI), `cost_per_row`, `cost_vs_reference`, `monthly_delta_usd`, `error_rate`,
   and the `warnings`. The verdict is the exit code.

## Exit codes

`0` pass (without `--ci`: finished, not a verdict) · `1` gate failed · `2` usage or config
error · `3` preflight failed or a provider extra is missing · `4` budget reached; re-run the
same command to resume.

## Do not

- Do not treat exit `0` without `--ci` as a pass.
- Do not reuse an `--out` folder across questions (`test split re-touched`, stale resumes).
- Do not use `--skip-preflight` to get past a preflight failure; fix the candidate.
- Do not raise `budget_usd` or re-run paid evals without asking.
- Do not read `$0` cost as free: it means no price was found. Add `[pricing."<model>"]`.
