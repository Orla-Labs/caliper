# Caliper

**Graders you can trust for RL fine-tuning: catch reward hacks before your model does.**

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python >=3.12](https://img.shields.io/badge/python-%3E%3D3.12-blue.svg)](./pyproject.toml)
[![tests: 329 passing](https://img.shields.io/badge/tests-329%20passing-brightgreen.svg)](#run-the-tests)

![Caliper demo: write a grader, dry-run it, mutation-test it, measure kappa, compile to TRL](docs/demo.gif)

## Why it matters

In reinforcement fine-tuning the grader *is* the objective. The policy is optimized against whatever the grader actually rewards, not what you meant it to reward. A grader that checks "is the gold answer somewhere in the response?" also pays full reward for listing three candidate numbers, for a bare number with no working, or for `1161.69` when the answer is `161.69`. The model will find those holes faster than you do.

Caliper treats the grader as code under test:

- **Mutation testing.** Eight built-in adversarial edits (hardcode the gold answer, truncate, corrupt the answer, add sycophancy, ...) each declare how a robust grader's score should move. Mutations that survive are reward hacks waiting to happen.
- **Agreement with humans.** Cohen's kappa against a small labeled set, next to a surface-feature baseline, so you know whether the grader agrees with people for the right reasons.
- **One grader, several trainers.** The same `@grader` function compiles to a self-contained reward module for TRL or Amazon Bedrock RFT.

On the bundled example (`examples/invoice_totals`, 46 human-labeled answers), the naive grader lets 2 mutations survive and has kappa 0.389. The fixed grader kills every applicable mutation and reaches kappa 0.868. `scripts/demo.sh` reproduces those numbers byte for byte.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) ≥ 0.11, and for the editor, Node 20+ with pnpm.

```bash
uv sync

# The whole story in the terminal: dataset -> mutation tests -> kappa -> compile to TRL.
# Offline, no API keys, deterministic.
scripts/demo.sh
scripts/demo.sh --check        # diff against demo/expected_output.txt

# Browser editor + dry-run server
pnpm --dir packages/editor install
scripts/dev.sh                 # editor http://localhost:3456, server http://localhost:8000

# Compile a grader for your trainer
uv run caliper-compile --target trl \
    --grader examples/invoice_totals/graders.py:fixed_grader \
    --dataset examples/invoice_totals/prompts.jsonl \
    --out build/trl
```

## Supported targets

`caliper-compile` turns the grader you debugged in Caliper into the reward function your trainer runs. It also exports your prompts and gold answers as that trainer's dataset. Each emitted reward file is self-contained: it holds your grader's source plus a small standard-library copy of `grader`/`Rollout`/`GraderResult`. It therefore runs on a TRL training box or in AWS Lambda without Caliper or pydantic installed. Output is deterministic, so you can diff and review it like code. See [docs/targets.md](docs/targets.md).

| Target | Trainer | Emits | Status |
|---|---|---|---|
| `trl` | Hugging Face TRL `GRPOTrainer` (any open-weight model) | `train.jsonl` (conversational `prompt` + `gold`), `reward_funcs.py` | Supported |
| `bedrock` | Amazon Bedrock reinforcement fine-tuning (Nova 2 Lite, gpt-oss-20B, Qwen3 32B) | `train.jsonl` (`messages` + `reference_answer`), `lambda_function.py` | Supported |
| `openai` | OpenAI reinforcement fine-tuning | `rft_job.json` (python grader), `train.jsonl` | Legacy / deprecated: [OpenAI is winding down fine-tuning](https://developers.openai.com/api/docs/deprecations) |

## Built by

Caliper is built by [Orla Labs](https://orlalabs.com).

## Layout

```
.
├── packages/
│   ├── sdk/          @grader decorator, Runner, typed records, source loader   (Python)
│   ├── store/        versioned content-addressed eval-set storage (SQLite/PG)  (Python)
│   ├── rft_compile/  caliper-compile: grader -> TRL / Bedrock / OpenAI (legacy) (Python)
│   ├── differ/       v1 vs v2 grader diff on an eval set                       (Python)
│   ├── monitor/      reward-hack auxiliary judges (4 built-in rubrics)         (Python)
│   ├── mutate/       mutation testing for graders (8 built-ins)                (Python)
│   ├── kappa/        Cohen's κ between a grader and a labeled reference set   (Python)
│   ├── server/       FastAPI dry-run backend + pluggable executor (in-proc/E2B)(Python)
│   └── editor/       Monaco-based grader IDE                                   (Next.js)
├── examples/
│   └── invoice_totals/  golden dataset, human labels, naive/fixed/baseline graders
├── demo/
│   ├── run_demo.py         the deterministic end-to-end demo (scripts/demo.sh)
│   ├── expected_output.txt its byte-exact transcript
│   └── record/             re-records docs/demo.gif (Playwright + ffmpeg)
├── docs/
│   ├── demo.gif
│   └── targets.md          per-target details for caliper-compile
└── scripts/
    ├── demo.sh                     runs demo/run_demo.py; --check diffs against the transcript
    ├── dev.sh                      starts server (:8000) + editor (:3456) together
    ├── smoke_dry_run.py            POSTs sample grader → /dry-run, asserts shape
    ├── smoke_mutation_test.py      runs all 8 mutations against robust + naked-gold graders
    ├── smoke_kappa_tracking.py     legal grader vs hand-built labels; prints unweighted/linear/quadratic κ
    ├── smoke_compile_targets.py    caliper-compile for trl/bedrock/openai; runs each artifact in `python -I -S`
    ├── smoke_rft_passthrough.py    legacy OpenAI path: compile → submit → poll via FakeRFTClient
    └── smoke_e2b_sandbox.py        live smoke for E2BExecutor; skips without E2B_API_KEY
```

## Editor and dry-run server

```bash
./scripts/dev.sh
# editor: http://localhost:3456
# server: http://localhost:8000
```

Open the editor, edit the sample grader, click **Dry run**: see the histogram, top/bottom rows, and any failures. Append `?evalSet=<name>` to the editor URL to dry-run against another eval set in the server's store.

Or in pieces:

```bash
uv run caliper-server --port 8000
pnpm --dir packages/editor dev
```

## Run the tests

```bash
uv sync
uv run pytest -q              # 329 tests
uv run ruff check packages
```

## Smokes

Self-contained end-to-end checks. No network, no API keys (one optional smoke skips cleanly without `E2B_API_KEY`).

```bash
uv run python scripts/smoke_dry_run.py
uv run python scripts/smoke_mutation_test.py
uv run python scripts/smoke_kappa_tracking.py
uv run python scripts/smoke_compile_targets.py
uv run python scripts/smoke_rft_passthrough.py    # legacy OpenAI path
uv run python scripts/smoke_e2b_sandbox.py        # SKIPs without E2B_API_KEY
scripts/demo.sh --check
```

## What's interesting in here

- **Mutation testing for graders** (`packages/mutate/`): 8 built-in mutations (drop-format-gate, wrong-answer, truncate, whitespace-perturb, prepend-sycophancy, append-garbage, case-perturb, hardcode-gold) plus a rank-order CI gate. Surfaces graders that pass on the surface but accept reward-hacked outputs.
- **Cohen's κ tracking** (`packages/kappa/`): unweighted / linear-weighted / quadratic-weighted κ between a grader and a labeled reference set, with an explicit `kappa_undefined` flag for the math-undefined cases. Pure stdlib + Pydantic.
- **Pluggable grader executor** (`packages/server/src/caliper_server/executor.py`): `InProcessExecutor` for offline tests; `E2BExecutor` runs grader source inside a Firecracker microVM with the workspace SDK shipped in as a wheel. Selected via `CALIPER_GRADER_EXECUTOR=in_process|e2b`.
- **Versioned content-addressed eval-set storage** (`packages/store/`): `EvalSetStore` Protocol, two backends (`InMemoryStore` + `SqlStore` via SQLAlchemy 2.x over SQLite or Postgres), 12-char SHA-256 content hashes, idempotent `create()`. Selected via `CALIPER_STORE_URL`.
- **Compile to trainers** (`packages/rft_compile/`): `caliper-compile` emits a self-contained reward module plus dataset for TRL `GRPOTrainer` or an AWS Lambda for Amazon Bedrock RFT, byte-identical across runs. The legacy OpenAI path (`method.type=reinforcement` JSON, `RFTRunner` + `OpenAIRFTClient` with mocked-SDK tests) is kept for existing users while OpenAI winds down fine-tuning.

## Stack

- Python ≥ 3.12, uv workspace, ruff, pytest with `--import-mode=importlib`
- TypeScript / pnpm / Next.js 15 (app router) / React 19 / `@monaco-editor/react`
- Pydantic v2 for all typed records
- SQLAlchemy 2.x for the eval-set store; `e2b-code-interpreter` as an optional `[e2b]` extra

## License

[Apache-2.0](./LICENSE).
