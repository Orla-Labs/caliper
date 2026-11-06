# 02 — Technical State of the Art (Late 2025 / Early 2026)

## The compressed history

Sebastian Raschka's arc, now consensus:

| Year | Era |
|---|---|
| 2022 | RLHF + PPO |
| 2023 | LoRA SFT |
| 2024 | Mid-Training |
| 2025 | RLVR + GRPO |
| 2026 | Inference-time scaling (predicted) |
| 2027 | Continual learning (predicted) |

After DeepSeek-R1 (arXiv 2501.12948, Jan 2025), every frontier reasoning model in production — o-series, R1, Qwen3, Gemini Deep Think, DeepSeek V3.2-Speciale, GPT-OSS, Llama 4 — uses **RLVR (reinforcement learning with verifiable rewards)** on top of a strong base model, optimized with a **GRPO-family algorithm**.

Nathan Lambert: *"OpenAI's RFT is a much better name for the method than our reinforcement learning with verifiable rewards, RLVR."* They refer to the same thing.

## GRPO mechanics (why it took over)

For each prompt:

1. Sample a group of G rollouts.
2. Compute advantages by mean/std-normalizing rewards within the group.
3. Apply a PPO-style clipped surrogate loss.

**The critic disappears**, halving memory vs PPO. This is why it took over for reasoning models where rewards are binary or rule-based.

## The default 2026 recipe

| Algorithm | When to use | Source |
|---|---|---|
| **GRPO** | Baseline; reasoning + verifiable rewards | DeepSeek, arXiv 2402.03300 |
| **GRPO + DAPO tricks** | Production default for dense models | ByteDance Seed, arXiv 2503.14476 |
| **GSPO** | MoE training (token-level IS unstable on MoE) | Qwen, arXiv 2507.18071 |
| **Dr.GRPO** | Token efficiency matters | Removes length/difficulty bias |

DAPO's four tricks worth knowing:

1. **Clip-Higher** — asymmetric clipping prevents entropy collapse.
2. **Dynamic Sampling** — drop zero-gradient prompts (all rollouts same reward = no signal).
3. **Token-level loss** — fixes GRPO's bias toward short responses.
4. **Overlong reward shaping** — soft penalty rather than hard truncation.

## Algorithm is commoditized

verl, TRL, OpenRLHF, NeMo-RL, and ART each ship every variant within ~30 days of publication. Do not write a new GRPO trainer.

## The agent frontier (multi-turn credit assignment)

Single-turn → multi-turn is the active research front. None is a clean turnkey solution for trajectories of 50+ turns.

| Approach | What it does | Source |
|---|---|---|
| **MT-GRPO** | Per-turn advantage estimation | Zeng/Wei/Brown et al., arXiv 2505.11821 |
| **VAGEN** | Bi-level GAE propagates turn → token | vagen-ai.github.io |
| **HISR** | Retroactively weights process rewards by outcome | — |

## The practitioner consensus

The algorithm is no longer the bottleneck. The environment and the reward function are.

- **Will Brown (Prime Intellect, Latent Space):** *"Environment construction is the new data labeling."*
- **Ross Taylor (ex-Llama, General Reasoning, TechCrunch):** *"I think people are underestimating how difficult it is to scale environments. Even the best publicly available [RL environments] typically don't work without serious modification."*
- **Kyle Corbitt (OpenPipe, RULER launch):** *"[RL's] adoption has been limited by a critical bottleneck: the need for bespoke, high-quality reward functions."*

This is the single most important fact in this knowledge base. The wedge follows from it.

## Open problems that gate the market

| Problem | Why it matters | Source |
|---|---|---|
| **Reward hacking / misspecification** | Anthropic Nov 2025 (arXiv 2511.18397): reward hacking on harmless tasks generalizes to broader misalignment | Production-grade demand for hack detection |
| **Sample efficiency in long-horizon agents** | Trajectories of 50+ turns are still hand-crafted | Multi-turn credit assignment unsolved |
| **Off-policy correction in async rollouts** | Disaggregated rollouts introduce staleness | verl AgentLoop, OpenRLHF async |
| **Base-model ceiling** | RL "amplifies behaviors learned in pretraining" (arXiv 2504.07912); induces highly sparse parameter updates (arXiv 2507.17107) | Don't pitch "out-reason the frontier" |

## The reward attack surface

Known anti-patterns documented in production:

- Anthropic's Claude variants learned to monkey-patch test code or hardcode answers ("Sycophancy to Subterfuge" paper).
- Multi-turn RL agents learn dummy tool calls when "tool used" is rewarded without verifying utility.
- GPT-4o's May 2025 sycophancy rollback was an RM gaming the agreeability dimension.
- Lambert's "RL with random rewards" piece: apparent RLVR gains on Qwen-2.5 turned out to be format-shaping, not reasoning. **Bad graders make RL look like it's working when it isn't.**

The grader is an attack surface. Reward-hacking detection is genuinely unsolved infrastructure.

## The 70/30 inference-vs-training rule

**70–90% of an RL job's wall-clock is sample generation, not gradient updates.**

Source: HF "Keep the Tokens Flowing" survey (March 2026); verl paper.

Implications:

- Inference efficiency is your training-cost lever.
- Networking requirements are far below pretraining — Ethernet/RoCE on a neocloud is fine for ≤32B with LoRA + GRPO.
- Disaggregated, asynchronous rollouts with bounded staleness is the architectural direction (verl AgentLoop, OpenRLHF async, ART, AReaL).

## OSS framework landscape (production reality)

| Framework | Status | When to use |
|---|---|---|
| **verl** (ByteDance/Volcano) | Production crown — HybridFlow, EuroSys 2025; trains Seed-Thinking-v1.5, Doubao | Distributed, multi-cloud, ≥1 node, large models |
| **TRL** (HuggingFace) | OSS reference, ~18K stars; v1.0 April 2026; GRPO/GSPO/DAPO/RLOO native, vLLM-accelerated | Compatibility fallback, single-node-friendly |
| **ART** (OpenPipe → CoreWeave) | Easiest agent-RL on-ramp for 1–8 GPU; Apache-2.0 | Easy-mode for early customers |
| **NeMo-RL** (NVIDIA) | NVIDIA-aligned, Megatron-Core | Enterprise NVIDIA shops |
| **OpenRLHF** | Ray-based, async-first, DAPO/REINFORCE++ | Async/agent workloads |
| **SkyRL**, **prime-rl** | Niche/research | Specialized cases |
| **TorchTune** | **Deprecated** — Meta redirected to torchforge | Don't build on this |
| **DeepSpeed-Chat** | Legacy | Don't build on this |

## Inference layer convergence

| Engine | Strengths | Use for |
|---|---|---|
| **vLLM** | PagedAttention, continuous batching, multi-LoRA, ecosystem | **Primary rollout engine** |
| **SGLang** | Structured generation, multi-turn, VLM | Agent/structured workloads |
| **TensorRT-LLM** | NVIDIA-optimized | Production inference (not training rollouts) |

Weight sync uses NCCL-broadcast; Mooncake RDMA for trillion-param refresh. Training: FSDP2 or Megatron-Core.

## Build-vs-buy answer for an MVP

**Do not write a new GRPO trainer.**

- Production backend: **verl**
- Easy mode for early customers: **ART**
- Compatibility fallback: **TRL**

The wedge is not the trainer. It's what sits above and beside it (graders, reward-hack regression, observability, multi-backend orchestration).

## 9 commercial gaps OSS leaves open

Pick 3 of 9 for the MVP:

1. Managed disaggregated orchestration as a service
2. Multi-cloud GPU brokerage with automatic spot recovery for RL
3. **Graders / reward models as a service with quality SLAs ← pick this**
4. **RL-aware observability and debugging (the missing TensorBoard for RL) ← pick this**
5. Full-lifecycle multi-LoRA fleet management
6. Sandbox/environment infrastructure with multi-tenant SOC2
7. Distributed reward-function execution with caching/batching
8. RL data curation and synthetic data
9. Bayesian RL hyperparameter search aware of KL collapse and reward hacking

**Recommendation:** 3 + 4 + a thin orchestration shell that calls verl/ART/RFT (a sliver of 1).
