"""Compile targets: turn a caliper ``@grader`` + EvalRow dataset into the
artifacts a training stack consumes.

Targets:
    trl      Hugging Face TRL GRPOTrainer: train.jsonl + reward_funcs.py
    bedrock  Amazon Bedrock reinforcement fine-tuning: train.jsonl + lambda_function.py
    openai   LEGACY / deprecated: OpenAI RFT job payload (rft_job.json + train.jsonl).
             OpenAI is winding down its fine-tuning platform:
             https://developers.openai.com/api/docs/deprecations

CLI: ``caliper-compile`` (see :mod:`caliper_rft.cli`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from caliper_sdk import EvalRow

from caliper_rft.targets._common import CompileError, GraderSource, load_dataset
from caliper_rft.targets.bedrock import export_bedrock
from caliper_rft.targets.openai_legacy import export_openai
from caliper_rft.targets.trl import export_trl, to_trl_reward_func

Exporter = Callable[..., list[str]]

_EXPORTERS: dict[str, Exporter] = {
    "trl": export_trl,
    "bedrock": export_bedrock,
    "openai": export_openai,
}
TARGETS: tuple[str, ...] = ("trl", "bedrock", "openai")


def compile_target(
    target: str,
    grader: GraderSource,
    rows: list[EvalRow],
    out_dir: str | Path,
    *,
    on_error: float | None = 0.0,
) -> list[str]:
    """Emit ``target`` artifacts into ``out_dir``; return the written file names."""
    if target not in _EXPORTERS:
        raise CompileError(f"target {target!r} is not available (known: {', '.join(TARGETS)})")
    return _EXPORTERS[target](grader, rows, out_dir, on_error=on_error)


__all__ = [
    "TARGETS",
    "CompileError",
    "GraderSource",
    "compile_target",
    "export_bedrock",
    "export_openai",
    "export_trl",
    "load_dataset",
    "to_trl_reward_func",
]
