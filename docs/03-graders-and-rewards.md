# 03 — Graders and Rewards (The Real Product Surface)

## Grader taxonomy

The post-training ecosystem has converged on roughly eight grader families, almost always composed:

| Family | What it does | Example |
|---|---|---|
| **Programmatic** | Regex, math equality, JSON-schema, format checks | "Did the response start with `<think>`?" |
| **Code execution** | Run generated code against tests in a sandbox | LeetCode-style; SWE-Bench |
| **Reference-based** | Gold answer match, F1, ROUGE, embedding similarity | "Does output match gold within ε?" |
| **Model-graded / LLM-as-judge** | LLM scores a rubric or rates a response | "Is this answer factually correct?" (judge model) |
| **Rubric / constitution-based** | Explicit binary criteria scored by judge | "Did response cite a source? (Y/N)" × N criteria |
| **Trained reward models** | Bradley-Terry RM trained on preference data | Classic RLHF |
| **Tool-use trace verification** | Was the right tool called, did SQL execute | Agent grading |
| **Eval-as-reward / relative ranking** | Rank N rollouts pairwise rather than absolute | RULER |

## OpenAI's RFT JSON spec — the de facto interchange format

Every serious entrant must speak this:

```json
{
  "type": "multi",
  "graders": {
    "format": {"type": "string_check", "operation": "starts_with", "reference": "<think>"},
    "correctness": {"type": "score_model", "model": "gpt-4.1", "prompt": "..."},
    "code_runs": {"type": "python", "source": "..."}
  },
  "calculate_output": "0.3 * format + 0.5 * correctness + 0.2 * code_runs"
}
```

Native types: `string_check`, `text_similarity`, `score_model`, `python`, `label_model`, `multi`.

**This format is the lingua franca.** Build your IDE to import/export it.

## What makes a grader robust (= SDET disciplines)

| Principle | Why | Source |
|---|---|---|
| **Prefer rule-based when possible** | "The neural reward model may suffer from reward hacking in the large-scale reinforcement learning process" | DeepSeek-R1 paper |
| **Compose graders with explicit weights** | Single-signal rewards get gamed | OpenAI RFT cookbook |
| **After observing reward-hacking, *reduce* the weight of the lexical proxy and add contrastive examples** | Standard pattern in HealthBench cookbook | OpenAI |
| **Use binary, not Likert** | 1–5 scores create unreproducible labels; binary forces a decision boundary | Hamel Husain |
| **Penalize tokens** | "The more tokens you use, the worse it is — that's the one rule above all" | Will Brown |
| **Validate the validator** | Target Cohen's κ ≥ 0.6 against human labels before deploying as reward | Eugene Yan, Shreya Shankar (EvalGen) |

## Reward hacking — the unsolved attack surface

| Documented case | What happened |
|---|---|
| Anthropic Claude variants | Learned to monkey-patch test code or hardcode answers ("Sycophancy to Subterfuge") |
| Multi-turn RL agents | Learn dummy tool calls when "tool used" is rewarded without verifying utility |
| GPT-4o May 2025 rollback | RM gamed the agreeability dimension |
| Qwen-2.5 "RL with random rewards" | Apparent RLVR gains turned out to be format-shaping, not reasoning |
| Anthropic Nov 2025 (arXiv 2511.18397) | Reward hacking on harmless tasks generalizes to broader misalignment |

**The grader is an attack surface.** Reward-hacking detection is genuinely unsolved infrastructure. This is a productizable demand surface, not just a quality concern.

## Multi-step / multi-turn grading

State of the art (none turnkey for 50+ turns):

| Approach | Mechanism |
|---|---|
| **MT-GRPO** | Per-turn advantage estimation |
| **VAGEN bi-level GAE** | Propagates turn-level advantages to tokens |
| **HISR** | Retroactively weights process rewards by outcome |
| **Process reward models (PRMs)** | Score each step; expensive to label |
| **Outcome-only with credit propagation** | Cheap; loses fine-grained signal |

Practitioner default: outcome-only + RULER-style ranking for the easy cases; PRMs only when stakes justify the labeling cost.

## SDET → grader engineering map (the wedge)

| SDET discipline | Grader/RFT analog | Productizable? |
|---|---|---|
| Test design rigor | Eval-set construction | Yes — UI for equivalence classes, boundary cases |
| Verification mindset | Reward-hack detection | Yes — monitor rubrics |
| Regression testing / golden datasets | Held-out eval sets | Yes — eval-set versioning |
| Sandboxed execution | Grader runtime | Yes — multi-tenant E2B/Daytona |
| Flaky test management | RL reward variance | Yes — variance metrics, stability scoring |
| Test pyramid | Heuristic → LLM-judge → human | Yes — cost-aware grader composition |
| Contract testing | Function-call grading | Yes — OpenAPI-style schema tests |
| **Mutation testing** | **Grader robustness — almost no one does this** | **Yes — the wedge** |
| CI/CD for ML | Continuous post-training pipelines | Yes — webhook + auto-eval on every model push |

**Mutation testing of graders** is the productizable wedge nobody else is building. Mutate model outputs in adversarial ways; ensure the grader rank-orders correctly. This is exactly what an SDET would build first.

## Grader composition patterns

Default 80/20 patterns to ship in the SDK:

```python
# Pattern 1: Format gate + correctness
@grader
def reasoning_grader(prompt, response, gold):
    if not response.startswith("<think>"):
        return 0.0  # hard fail
    answer = extract_answer(response)
    return 1.0 if answer == gold else 0.0

# Pattern 2: Composite weighted
@grader
def support_agent_grader(prompt, response, trace):
    return (
        0.3 * format_check(response) +
        0.4 * tool_use_correctness(trace) +
        0.3 * llm_judge_helpfulness(prompt, response)
    )

# Pattern 3: RULER-style relative
@grader
def relative_grader(prompt, rollouts):
    return judge_rank(prompt, rollouts)  # returns ranks, not absolute scores
```

## Authoring DX requirements (the IDE)

| Feature | Why |
|---|---|
| **Monaco editor** | VSCode parity; familiar to the buyer |
| **Typed Python Grader SDK** | Catch errors at edit time, not in a 6-hour training run |
| **Hot reload** | Change grader, see new reward in <1s |
| **Dry-run on eval set** | Run grader on 100 historical rollouts, see the distribution |
| **Diff vs prior version** | Which prompts now pass/fail differently |
| **Mutation testing** | Inject adversarial outputs, ensure grader catches them |
| **Reward-hack monitor rubrics** | Auxiliary judges that fire on suspicious patterns |
| **Regression tests of graders themselves** | Every grader change runs against historical rollouts |
| **Cohen's κ tracking** | vs. labeled human reference set |
| **Distribution dashboards** | Reward histogram over time; drift alerts |

This is the surface area no incumbent owns coherently.

## Validate-the-validator workflow

Adapted from EvalGen / Hamel Husain:

1. Founder/customer hand-labels 50–200 examples binary (pass/fail).
2. Author candidate grader.
3. Compute Cohen's κ vs. labels. **If κ < 0.6, do not use as reward.**
4. Iterate grader until κ ≥ 0.6.
5. Hold out 20% as a regression set.
6. Every grader change re-runs against the held-out set. Track κ over time.

This entire workflow is what the IDE automates.
