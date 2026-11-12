## Caliper (model and prompt change evaluation)

This repo uses [Caliper](https://caliper.orlalabs.com/agents/) (`pip install orlalabs-caliper`) to
check that a change to a model, prompt or model settings does not make an AI feature worse or
more expensive. Agent docs: https://caliper.orlalabs.com/agents/ (any page + `.md` for
Markdown; everything in one file: https://caliper.orlalabs.com/llms-full.txt).

- Eval config: `<path/to/caliper.toml>`. Golden set: `<path/to/golden.jsonl>`. Feature logic:
  `<path/to/decide.py>`. Edit these placeholders to match this repo.
- Run: `caliper eval --config <path/to/caliper.toml> --out build/caliper-<timestamp> --ci --summary-md build/summary.md`.
- **Exit codes:** `0` pass, `1` gate failed (reasons printed per candidate), `2` usage or config
  error, `3` preflight failed or provider extra missing, `4` budget reached (re-run to resume).
  Without `--ci` the exit code is `0` even when a candidate regresses.
- **Results:** parse `OUT/report.json`
  ([schema](https://caliper.orlalabs.com/schemas/eval-report.schema.json)), not stdout.
- **Money:** candidates whose `provider` is not `fake` make paid API calls. Ask before running
  them, keep `budget_usd` set, and never raise it without asking. `caliper example` and `fake`
  candidates are free and offline.
- Use a fresh `--out` for each question: a reused one resumes old results, and a second `--ci`
  run into it fails with `test split re-touched`.
- After editing `decide.py`, re-run with `--no-resume`.
- A model with no known price costs `$0` and is flagged `unpriced`; add `[pricing."<model>"]`
  in USD per million tokens.
- To show a costlier candidate is *better*, not just different: `[ci] quality_per_cost` plus
  `[quality]` approvals. `caliper review` lists the outcomes to judge; a person labels them
  (`caliper label`). Never write approvals yourself or with a model.
