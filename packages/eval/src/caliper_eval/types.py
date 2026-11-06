"""Shared data contract for caliper-eval (Phase 1).

This module is the interface between the eval engine (providers, runner,
budget, results), the analysis layer (decision, noise floor, transitions,
calibration, CI gate) and the examples. Change it only in coordination:
every slice imports from here.

Vocabulary
----------
* GoldenRow   - one input to the feature, holding the request *as sent* in
                production (byte-faithful replay), plus optional reference
                outcome and human label.
* Candidate   - model + thinking mode + structured-output mode on a provider.
* Reply       - one model response, including who actually served it.
* Decision    - the *system* outcome: the user's ``decide(replies)`` turns the
                replies for one row into what the feature would have done.
* RowResult   - one (candidate, row, replica) execution; one JSONL line in the
                resumable results file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def canonical_json(obj: Any) -> str:
    """Stable JSON used for hashing and for results files."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


def candidate_sha256(candidate: Candidate) -> str:
    """Hash of everything that defines a candidate (model, modes, schema, params...)."""
    return request_sha256(candidate.model_dump(mode="json"))


# --------------------------------------------------------------------------- golden set


class GoldenRow(BaseModel):
    """A golden-set row. One JSONL line in a golden file.

    ``request`` is provider-neutral and stored exactly as the feature sent it:
    ``{"system": str | None, "messages": [{"role", "content"}], "max_tokens": int,
    ...any other params}``. A Candidate overlays model / thinking /
    structured-output settings at send time; nothing else is rewritten.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    request: dict[str, Any]
    reference_outcome: str | None = None  # outcome production recorded, if known
    label: str | None = None  # human-approved outcome, if labeled
    label_approved: bool | None = None  # human approved the feature's output (calibration)
    stratum: str | None = None  # e.g. "positive" / "unsure" / "negative" from the sampler
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- candidates


class ProviderKind(StrEnum):
    FAKE = "fake"
    ANTHROPIC = "anthropic"  # Anthropic API direct
    BEDROCK = "bedrock"  # Anthropic models on Amazon Bedrock
    OPENAI_COMPAT = "openai_compat"  # any OpenAI-compatible chat-completions endpoint


class ThinkingMode(StrEnum):
    OFF = "off"
    ON = "on"  # extended/adaptive thinking; budget in Candidate.thinking_budget_tokens


class OutputMode(StrEnum):
    TEXT = "text"
    JSON_SCHEMA = "json_schema"  # provider-native structured output against output_schema
    TOOL = "tool"  # forced single tool call whose input is the structured output


class Candidate(BaseModel):
    """What is being evaluated. The identity is the whole tuple, not the model."""

    model_config = ConfigDict(extra="forbid")

    name: str  # unique within a run, e.g. "baseline", "haiku-nothink"
    provider: ProviderKind
    model: str
    thinking: ThinkingMode = ThinkingMode.OFF
    thinking_budget_tokens: int | None = None
    output_mode: OutputMode = OutputMode.TEXT
    output_schema: dict[str, Any] | None = None
    replies_per_row: int = Field(default=1, ge=1)  # >1 for vote loops
    params: dict[str, Any] = Field(default_factory=dict)  # temperature etc.
    base_url: str | None = None  # openai_compat
    region: str | None = None  # bedrock
    fake_script: str | None = None  # fake: "path/to/file.py:func"


# --------------------------------------------------------------------------- replies


class ServedBy(BaseModel):
    """Who actually answered. Never inferred from the request."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderKind
    model: str  # as reported by the response, not as requested
    endpoint: str | None = None  # base URL / region / "fake"
    matches_request: bool  # False => fallback/substitution happened; surfaced in reports


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    parsed: Any = None  # structured output, when output_mode != text
    served_by: ServedBy | None = None
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None  # provider/transport/parse error; text/parsed may be None


# --------------------------------------------------------------------------- decisions


class Decision(BaseModel):
    """The system outcome for one row, as the real feature would have decided."""

    model_config = ConfigDict(extra="forbid")

    outcome: str  # categorical outcome; transitions are computed on this
    score: float | None = None  # confidence score in [0, 1] for calibration, if any
    detail: dict[str, Any] = Field(default_factory=dict)


DecideFn = Callable[[list[Reply]], Decision]


# --------------------------------------------------------------------------- results


class RowResult(BaseModel):
    """One line of the resumable results JSONL. Key: (candidate, row_id, replica)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    candidate: str
    row_id: str
    replica: int = 0  # replay index; the reference is replayed >= 2x for the noise floor
    request_sha256: str  # hash of GoldenRow.request (pre-overlay)
    candidate_sha256: str | None = None  # hash of the Candidate settings (stale-resume check)
    replies: list[Reply]
    decision: Decision | None = None  # None if decide() raised
    error: str | None = None  # decide() error or row-level failure

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.candidate, self.row_id, self.replica)


# --------------------------------------------------------------------------- pricing


class Price(BaseModel):
    """USD per million tokens. Cache-aware."""

    model_config = ConfigDict(extra="forbid")

    input: float
    output: float
    cache_read: float | None = None  # defaults to input if None
    cache_write: float | None = None  # defaults to input if None
    source: str | None = None  # URL + date checked
