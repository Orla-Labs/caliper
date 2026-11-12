---
title: Introduction
description: What Caliper does and why it works the way it does.
---

Caliper is for teams shipping an LLM feature on a frontier API: classification, extraction, judging, scoring. These teams change prompts and models often. Before each change, `caliper eval` replays a golden set of real requests through the candidates. It applies your feature's own decision logic and tells you what the feature would do differently, what it would cost per call and per month, and whether the difference is larger than the noise you already live with. In CI it posts the verdict on the pull request and fails the build when a change regresses.

## Why it works this way

- **Grade the system outcome, not single replies.** Features wrap the model in vote loops, thresholds and fallbacks. You give Caliper your feature's `decide(replies) -> outcome`, and `assert_parity` proves that mirror matches the production rule over every reply sequence from a representative alphabet.
- **Noise floor first.** The reference model is replayed twice. A candidate's changed outcomes only count above the reference's own replay-flip rate. "Matches the incumbent" is a paired bootstrap of the per-row difference, not two separate intervals.
- **Cost-weighted transitions.** Every changed outcome is a transition (`auto_publish -> needs_review`). Each one carries the cost you assign, because a record that skips review is worse than one that gets an extra look.
- **A candidate is model + thinking + output mode.** A preflight fails fast when a provider rejects a request shape. Every result records which model actually answered, so a silent fallback fails the gate instead of passing it.
- **Cost is part of the verdict.** Every reply is priced from the provider's own published prices where one exists (the live AWS Price List for Bedrock, by region and inference tier). The report shows cost per row against the incumbent and, given your traffic, dollars per month. `[ci] max_cost_ratio` fails a change that costs too much, and a reply with no known price fails it too instead of reading as free.
- **Better, not just different.** Every other measure treats the incumbent as right, so it can only say "the same" or "worse". People's verdicts on outcomes show which side is right where the two differ, and only those rows need review: `caliper review` lists them, blind to the model, as a `caliper label` project. With `[ci] quality_per_cost = 1`, a candidate that costs 10% more must make at least 10% fewer errors, at 95%, or the gate fails it.
- **Calibration against human approvals.** If your feature emits a confidence score and people review some outputs, Caliper sweeps the auto-accept threshold on `val`, recommends the lowest one that reaches your target precision, and reports it on `test`.
- **Know who labelled what.** Every label carries its source. Agreement with an LLM teacher is reported as teacher agreement, never as accuracy. Hard cases get named slice gates of their own.

## What's in the package

| Command | What it does |
| --- | --- |
| `caliper eval` | Replay a golden set through candidates and gate CI ([guide](/eval/guide/)) |
| `caliper sample` | Build a golden set from production logs, keeping rare outcomes whole |
| `caliper label` | Label data in a local web UI ([guide](/label/overview/)) |
| `caliper kappa` | Inter-rater agreement, or a grader's agreement with human labels |
| `caliper example` | Copy a bundled example project to try the above |
| `caliper-compile` | Compile a grader into a reward function for TRL or Bedrock RFT ([targets](/graders/compile-targets/)) |

Caliper is open source under Apache-2.0: [github.com/Orla-Labs/caliper](https://github.com/Orla-Labs/caliper).
