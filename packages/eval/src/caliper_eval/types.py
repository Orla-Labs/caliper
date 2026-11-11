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

    id: str  # unique row id
    request: dict[str, Any]  # the request as the feature sent it (see the class docstring)
    reference_outcome: str | None = None  # outcome production recorded, if known
    label: str | None = None  # human-approved outcome, if labeled
    label_approved: bool | None = None  # human approved the feature's output (calibration)
    stratum: str | None = None  # e.g. "positive" / "unsure" / "negative" from the sampler
    metadata: dict[str, Any] = Field(default_factory=dict)  # free-form; slices can match on it
    label_source: LabelSource | None = None  # provenance of label/label_approved
    label_stage: str | None = None  # e.g. "triage", "final_review"; free-form
    split: Split | None = None  # selection hygiene: thresholds picked on val, test touched once
    slices: list[str] = Field(default_factory=list)  # named hard slices this row belongs to


# --------------------------------------------------------------------------- labels


class LabelSource(StrEnum):
    """Who produced a label. Agreement with LLM_TEACHER is 'teacher agreement', never accuracy."""

    HUMAN_END_USER = "human_end_user"  # the feature's real users acted on the item
    INTERNAL_REVIEWER = "internal_reviewer"  # staff labelling (e.g. via `caliper label`)
    LLM_TEACHER = "llm_teacher"  # an LLM's verdict
    HEURISTIC = "heuristic"  # a rule / weak label
    ADJUDICATED = "adjudicated"  # final label set by an adjudicator after a disagreement

    @property
    def is_human(self) -> bool:
        return self in (
            LabelSource.HUMAN_END_USER,
            LabelSource.INTERNAL_REVIEWER,
            LabelSource.ADJUDICATED,
        )


class Split(StrEnum):
    TRAIN = "train"
    VAL = "val"
    TEST = "test"


class LabelRecord(BaseModel):
    """One labelling act. Append-only; several records per item when double-labelled."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    decision: str | None  # a configured decision key's value; None when skipped
    skipped: bool = False
    labeller: str
    source: LabelSource = LabelSource.INTERNAL_REVIEWER
    stage: str | None = None
    labelled_at: str  # ISO-8601 UTC, e.g. "2026-11-06T12:00:00Z"
    queue: str | None = None
    category: str | None = None  # for inter-rater kappa by category
    note: str | None = None
    undoes: str | None = None  # record_id this record retracts (undo), if any
    record_id: str  # stable unique id (e.g. sha256 of the other fields), assigned by the writer


# --------------------------------------------------------------------------- candidates


class ProviderKind(StrEnum):
    FAKE = "fake"
    ANTHROPIC = "anthropic"  # Anthropic API direct
    BEDROCK = "bedrock"  # Anthropic models on Amazon Bedrock (Messages API, Mantle)
    BEDROCK_CONVERSE = "bedrock_converse"  # any Bedrock model via the Converse API (Nova, ...)
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
    provider: ProviderKind  # fake is offline and free; the others call (and bill) a provider
    model: str  # the provider's model id, e.g. a Bedrock inference profile id
    thinking: ThinkingMode = ThinkingMode.OFF  # extended/adaptive thinking on or off
    thinking_budget_tokens: int | None = None  # thinking budget, where the provider takes one
    output_mode: OutputMode = OutputMode.TEXT  # text, provider-native JSON schema, or a tool call
    output_schema: dict[str, Any] | None = None  # JSON Schema for json_schema / tool output
    replies_per_row: int = Field(default=1, ge=1)  # >1 for vote loops
    # Extra request params (temperature etc.); openai_compat reads its key from the env var
    # named by params.api_key_env (default OPENAI_API_KEY).
    params: dict[str, Any] = Field(default_factory=dict)
    base_url: str | None = None  # openai_compat endpoint, or an Anthropic API base URL
    region: str | None = None  # AWS region for bedrock / bedrock_converse
    fake_script: str | None = None  # fake provider: "path/to/file.py:func" producing replies


# --------------------------------------------------------------------------- replies


class ServedBy(BaseModel):
    """Who actually answered. Never inferred from the request."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderKind  # provider that answered
    model: str  # as reported by the response, not as requested
    endpoint: str | None = None  # base URL / region / "fake"
    matches_request: bool  # False => fallback/substitution happened; surfaced in reports


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0  # uncached input tokens
    output_tokens: int = 0  # output tokens, reasoning included
    cache_read_tokens: int = 0  # input tokens read from the prompt cache
    cache_write_tokens: int = 0  # input tokens written to the prompt cache


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None  # the reply text, when there is one
    parsed: Any = None  # structured output, when output_mode != text
    served_by: ServedBy | None = None  # who answered, from the response
    usage: Usage = Field(default_factory=Usage)  # token counts from the response
    latency_ms: float = 0.0  # wall time of the request
    cost_usd: float = 0.0  # usage x price; 0.0 when no price was found (see report warnings)
    error: str | None = None  # provider/transport/parse error; text/parsed may be None


# --------------------------------------------------------------------------- decisions


class Decision(BaseModel):
    """The system outcome for one row, as the real feature would have decided."""

    model_config = ConfigDict(extra="forbid")

    outcome: str  # categorical outcome; transitions are computed on this
    score: float | None = None  # confidence score in [0, 1] for calibration, if any
    detail: dict[str, Any] = Field(default_factory=dict)  # free-form, from decide()


DecideFn = Callable[[list[Reply]], Decision]


# --------------------------------------------------------------------------- results


class RowResult(BaseModel):
    """One line of the resumable results JSONL. Key: (candidate, row_id, replica)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str  # the invocation that wrote this line
    candidate: str  # candidate name from the config
    row_id: str  # GoldenRow.id
    replica: int = 0  # replay index; the reference is replayed >= 2x for the noise floor
    request_sha256: str  # hash of GoldenRow.request (pre-overlay)
    candidate_sha256: str | None = None  # hash of the Candidate settings (stale-resume check)
    replies: list[Reply]  # replies_per_row replies, in order
    decision: Decision | None = None  # None if decide() raised
    error: str | None = None  # decide() error or row-level failure

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.candidate, self.row_id, self.replica)


# --------------------------------------------------------------------------- pricing


class Price(BaseModel):
    """USD per million tokens. Cache-aware."""

    model_config = ConfigDict(extra="forbid")

    input: float  # USD per million input tokens
    output: float  # USD per million output tokens (reasoning tokens bill as output)
    cache_read: float | None = None  # defaults to input if None
    cache_write: float | None = None  # defaults to input if None
    source: str | None = None  # URL + date checked

GoldenRow.model_rebuild()
