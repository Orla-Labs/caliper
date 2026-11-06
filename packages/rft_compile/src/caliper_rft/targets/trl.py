"""Hugging Face TRL target (GRPOTrainer custom reward functions).

Docs this is written against (TRL main, checked 2026-11):
    https://huggingface.co/docs/trl/main/en/grpo_trainer
        "Using a custom reward function": reward funcs receive `prompts`,
        `completions`, `completion_ids`, `trainer_state`, `log_extra`, `log_metric`
        and every dataset column except `prompt` as keyword arguments, and return
        a list of floats (None = not applicable for that sample).
    https://huggingface.co/docs/trl/main/en/dataset_formats
        standard (prompt: str) vs conversational (prompt: list of messages).
    https://github.com/huggingface/trl/blob/main/trl/trainer/grpo_trainer.py
        callables are logged as rewards/<name>/mean via the callable's name.

Dataset format choice: **conversational** (`prompt = [{"role": "user", "content": ...}]`).
TRL then applies the policy model's chat template, which is what every
open-weight instruct/chat model we target (Qwen3, gpt-oss, Llama-Instruct)
expects, and it is the same `messages` shape the Bedrock target emits, so both
targets share one prompt representation. The reward function accepts both
formats anyway.

Columns in train.jsonl: `prompt`, `gold` (str|null), `caliper_metadata`
(JSON-encoded string: keeps arbitrary per-row metadata exact, instead of letting
Arrow unify heterogeneous dicts into a struct with null-filled keys), and `id`.
Keep `remove_unused_columns=False` (the GRPOConfig default) so they reach the
reward function.

No trl / torch / datasets import anywhere in caliper.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from caliper_sdk import EvalRow

from caliper_rft.targets._common import (
    GraderSource,
    dumps,
    render_artifact,
    write_jsonl,
    write_text,
)
from caliper_rft.targets._runtime import make_trl_reward_func

TRAIN_FILE = "train.jsonl"
REWARD_FILE = "reward_funcs.py"


def _identifier(name: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name) or "caliper_grader"
    return out if not out[0].isdigit() else "_" + out


def to_trl_reward_func(
    grader: Callable[..., Any],
    *,
    name: str | None = None,
    on_error: float | None = 0.0,
) -> Callable[..., list[float | None]]:
    """Wrap a caliper ``@grader`` as a TRL GRPOTrainer reward function.

    The result has signature ``f(completions, **kwargs) -> list[float]``.
    ``prompts`` (str or message lists), ``completions`` (str or message lists),
    and the dataset columns ``gold`` / ``caliper_metadata`` (or ``metadata``)
    are mapped onto a ``Rollout``. Its ``__name__`` is the grader's name, which
    TRL uses in its logs. Grader failures score ``on_error`` (pass ``None`` to
    have TRL exclude the sample instead); ``f.scorer.failures`` counts them.
    """
    resolved = name or str(getattr(grader, "name", None) or getattr(grader, "__name__", "grader"))
    return make_trl_reward_func(grader, _identifier(resolved), on_error)


def trl_record(row: EvalRow) -> dict[str, Any]:
    r = row.rollout
    return {
        "id": row.id,
        "prompt": [{"role": "user", "content": r.prompt}],
        "gold": r.gold,
        "caliper_metadata": dumps(r.metadata),
    }


_TAIL = '''\
# ---------------------------------------------------------------------------
# TRL entry points
#
#   from reward_funcs import reward_funcs
#   trainer = GRPOTrainer(model=..., reward_funcs=reward_funcs,
#                         train_dataset=load_dataset("json", data_files="train.jsonl",
#                                                    split="train"), ...)
#
# Docs: https://huggingface.co/docs/trl/main/en/grpo_trainer
# ---------------------------------------------------------------------------
REWARD_FUNC = make_trl_reward_func(GRADER, {func_name!r}, ON_ERROR)
reward_funcs = [REWARD_FUNC]

__all__ = ["REWARD_FUNC", "reward_funcs"]
'''


def render_reward_funcs(grader: GraderSource, *, on_error: float | None = 0.0) -> str:
    return render_artifact(
        target="trl",
        grader=grader,
        on_error=on_error,
        tail=_TAIL.format(func_name=_identifier(grader.name)),
        docstring=(
            f"TRL GRPOTrainer reward function for caliper grader {grader.name!r}.\n\n"
            "Self-contained: runs without caliper installed. Use `reward_funcs` (a list)\n"
            "or `REWARD_FUNC` as GRPOTrainer(reward_funcs=...)."
        ),
    )


def export_trl(
    grader: GraderSource,
    rows: list[EvalRow],
    out_dir: str | Path,
    *,
    on_error: float | None = 0.0,
) -> list[str]:
    """Write ``train.jsonl`` + ``reward_funcs.py`` into ``out_dir``; return file names."""
    out = Path(out_dir)
    write_jsonl(out / TRAIN_FILE, [trl_record(r) for r in rows])
    write_text(out / REWARD_FILE, render_reward_funcs(grader, on_error=on_error))
    return [TRAIN_FILE, REWARD_FILE]
