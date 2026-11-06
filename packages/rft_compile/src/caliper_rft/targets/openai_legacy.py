"""OpenAI reinforcement fine-tuning target. LEGACY / DEPRECATED.

OpenAI is winding down its self-serve fine-tuning platform:
    https://developers.openai.com/api/docs/deprecations
        ("Update to OpenAI's self-serve fine-tuning": closed to organizations that
        have not previously run fine-tuning since 2026-05-07; active existing
        customers lose job creation on 2027-01-06)
    https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning
        ("OpenAI is winding down the fine-tuning platform."; o4-mini RFT base model
        shut down 2026-10-23)
Kept only for existing customers. Prefer the ``trl`` or ``bedrock`` targets.

Emits ``rft_job.json``: the ``POST /v1/fine_tuning/jobs`` body produced by
:func:`caliper_rft.build_payload`, with a ``python`` grader whose source is the
caliper shim + your grader + ``grade(sample, item)``. Python grader contract:
    https://developers.openai.com/api/docs/guides/graders
        grade(sample, item) -> float; sample["output_text"] is the model output;
        item holds the training row's keys; source < 256 kB; exceptions score 0.
``model`` / ``training_file`` are placeholders to fill in. Also emits
``train.jsonl`` (``messages`` + ``gold`` + ``caliper_metadata`` per row) that the
grader reads via ``item``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from caliper_sdk import EvalRow

from caliper_rft.runner import build_payload
from caliper_rft.spec import PythonGrader
from caliper_rft.targets._common import (
    GraderSource,
    dumps,
    render_artifact,
    write_jsonl,
    write_text,
)

JOB_FILE = "rft_job.json"
TRAIN_FILE = "train.jsonl"
MODEL_PLACEHOLDER = "<BASE_MODEL>"
TRAINING_FILE_PLACEHOLDER = "<UPLOADED_TRAINING_FILE_ID>"
MAX_SOURCE_BYTES = 256 * 1024

_TAIL = '''\
# ---------------------------------------------------------------------------
# OpenAI RFT python-grader entry point
# Docs: https://developers.openai.com/api/docs/guides/graders
# ---------------------------------------------------------------------------
_SCORER = Scorer(GRADER, GRADER_NAME, 0.0 if ON_ERROR is None else ON_ERROR)


def grade(sample, item):
    return openai_grade(_SCORER, sample, item)
'''


def openai_record(row: EvalRow) -> dict[str, Any]:
    r = row.rollout
    return {
        "messages": [{"role": "user", "content": r.prompt}],
        "gold": r.gold,
        "caliper_metadata": dumps(r.metadata),
    }


def render_grade_source(grader: GraderSource, *, on_error: float | None = 0.0) -> str:
    return render_artifact(
        target="openai",
        grader=grader,
        on_error=on_error,
        tail=_TAIL,
        docstring=f"OpenAI RFT python grader (LEGACY) for caliper grader {grader.name!r}.",
    )


def build_rft_job(grader: GraderSource, *, on_error: float | None = 0.0) -> dict[str, Any]:
    source = render_grade_source(grader, on_error=on_error)
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("python grader source exceeds OpenAI's 256 kB limit")
    return build_payload(
        PythonGrader(source=source),
        model=MODEL_PLACEHOLDER,
        training_file=TRAINING_FILE_PLACEHOLDER,
    )


def export_openai(
    grader: GraderSource,
    rows: list[EvalRow],
    out_dir: str | Path,
    *,
    on_error: float | None = 0.0,
) -> list[str]:
    """Write ``rft_job.json`` + ``train.jsonl`` into ``out_dir``."""
    out = Path(out_dir)
    job = build_rft_job(grader, on_error=on_error)
    write_text(out / JOB_FILE, json.dumps(job, sort_keys=True, indent=2) + "\n")
    write_jsonl(out / TRAIN_FILE, [openai_record(r) for r in rows])
    return [JOB_FILE, TRAIN_FILE]
