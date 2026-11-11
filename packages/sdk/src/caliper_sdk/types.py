"""Shared types for the Caliper grader stack.

Imported by the SDK itself, plus the differ, monitor, and rft_compile packages.
Keeping these in one place avoids version skew across packages.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Rollout(BaseModel):
    """A single model rollout to be graded.

    `gold` is optional because some graders are reference-free (LLM-as-judge,
    rubric-based). `trace` carries tool-call records for agent grading.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str  # what the model was asked
    response: str  # what it answered
    gold: str | None = None  # reference answer, for graders that use one
    trace: list[dict[str, Any]] = Field(default_factory=list)  # tool-call records
    metadata: dict[str, Any] = Field(default_factory=dict)  # free-form; human_label for kappa


class GraderResult(BaseModel):
    """The output of a single grader invocation on a single rollout."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    explanation: str | None = None
    components: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalRow(BaseModel):
    """A row in an eval set: a Rollout plus an identifier."""

    model_config = ConfigDict(extra="forbid")

    id: str  # unique row id
    rollout: Rollout  # the rollout to grade


class GraderCallable(Protocol):
    """Structural type that anything decorated with @grader satisfies."""

    name: str
    version: str

    def __call__(self, rollout: Rollout) -> GraderResult: ...
