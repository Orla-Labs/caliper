"""Eval configuration, loaded from TOML.

Relative paths (``golden``, ``decide``, candidates' ``fake_script``) resolve
against the directory holding the config file (``EvalConfig.config_dir``).
They are kept relative in the model itself so nothing machine-specific leaks
into results or reports.
"""

from __future__ import annotations

import hashlib
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from caliper_eval.types import Candidate, DecideFn, LabelSource, Price, canonical_json


class GateRequirement(StrEnum):
    """What the regression gate must be able to say about a candidate.

    Both read the paired bootstrap CI of ``excess = regression - replay noise``
    (see ``caliper_eval.transitions.compute_paired_excess``):

    * ``non_inferiority`` (default): pass only when the CI's **upper** bound is
      within ``max_regression``. A pass means "no worse than the reference
      beyond replay noise + max_regression, at 95%".
    * ``no_evidence_of_regression``: fail only when the CI's **lower** bound is
      above ``max_regression``. A pass then only means "no detectable
      regression"; small golden sets pass easily. Use knowingly.
    """

    NON_INFERIORITY = "non_inferiority"
    NO_EVIDENCE_OF_REGRESSION = "no_evidence_of_regression"


class CIConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_regression: float = 0.0  # allowed cost-weighted regression per row beyond replay noise
    require: GateRequirement = GateRequirement.NON_INFERIORITY  # which CI bound the gate reads
    # Share of results allowed to end with no decision; --max-error-rate overrides it.
    max_error_rate: float | None = Field(default=None, ge=0, le=1)
    # Fail a candidate whose mean cost per execution is above this multiple of the
    # reference's (1.0 = "no more expensive"). A candidate or reference with replies
    # priced at $0 fails too: an unknown price must not read as free.
    max_cost_ratio: float | None = Field(default=None, gt=0)
    # A candidate that costs more than the reference must be better by at least this
    # many times its extra cost, judged by human approvals (needs [quality]):
    # 1.0 means 10% more cost needs at least 10% fewer errors than the reference.
    quality_per_cost: float | None = Field(default=None, gt=0)


class CostProjectionConfig(BaseModel):
    """Production volume, to turn cost per row into dollars per month (``[cost_projection]``).

    ``calls_per_day`` counts feature calls, i.e. golden-row equivalents (a vote
    loop's 3 replies are one call).
    """

    model_config = ConfigDict(extra="forbid")

    calls_per_day: float = Field(gt=0)  # production feature calls per day
    days_per_month: float = Field(default=30.0, gt=0)


class OperatingPointConfig(BaseModel):
    """Matched-operating-point comparison (``[operating_point]``).

    ``auto_outcomes`` are the outcomes that count as "handled automatically"
    (auto-published, expensive call skipped, ...). The reference's share of
    those on val is the coverage the candidate's score threshold is matched to.
    """

    model_config = ConfigDict(extra="forbid")

    auto_outcomes: list[str] = Field(min_length=1)  # outcomes that mean "handled automatically"


class SliceRule(StrEnum):
    MAX_REGRESSION = "max_regression"  # paired excess bound on the slice <= threshold
    MUST_NOT_LOSE = "must_not_lose"  # zero rows lost vs the reference (matched op. point)
    MIN_PRECISION = "min_precision"  # human-truth precision of auto-handled rows >= threshold


class SliceConfig(BaseModel):
    """A named hard slice with its own pass rule (``[[slices]]``).

    A row belongs to the slice when ``name`` is in ``GoldenRow.slices`` or when
    it matches ``where`` (``{"stratum": "x"}`` / ``{"metadata.key": value}``;
    a list value means "any of"). ``label_sources`` restricts which label
    sources a label-based rule reads (default: ``min_precision`` reads human
    sources only, ``must_not_lose`` reads every labelled row).
    """

    model_config = ConfigDict(extra="forbid")

    name: str  # also matches rows whose `slices` list holds this name
    rule: SliceRule  # max_regression, must_not_lose or min_precision
    threshold: float | None = None  # required except for must_not_lose
    where: dict[str, Any] = Field(default_factory=dict)  # {"stratum": ...} / {"metadata.k": ...}
    label_sources: list[LabelSource] | None = None  # label sources a label-based rule reads

    @model_validator(mode="after")
    def _check(self) -> SliceConfig:
        if self.rule == SliceRule.MUST_NOT_LOSE:
            if self.threshold not in (None, 0, 0.0):
                raise ValueError(f"slice {self.name!r}: must_not_lose takes no threshold")
        elif self.threshold is None:
            raise ValueError(f"slice {self.name!r}: rule {self.rule.value} needs a threshold")
        if self.rule == SliceRule.MIN_PRECISION and not 0.0 <= (self.threshold or 0.0) <= 1.0:
            raise ValueError(f"slice {self.name!r}: min_precision threshold must be in [0, 1]")
        for key in self.where:
            if key != "stratum" and not key.startswith("metadata."):
                raise ValueError(
                    f"slice {self.name!r}: where key {key!r} must be 'stratum' or 'metadata.<key>'"
                )
        return self


class QualityConfig(BaseModel):
    """Human approvals of outcomes, to measure "better" (``[quality]``).

    See :mod:`caliper_eval.approvals` for the file format and what counts.
    """

    model_config = ConfigDict(extra="forbid")

    # Approval JSONL files, relative to the config file (`caliper review` builds them).
    approvals: list[str] = Field(default_factory=list)
    # Also count human golden labels: the labelled outcome is approved, any other rejected.
    golden_labels: bool = True


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    thresholds: list[float] | None = None  # score thresholds to report precision/coverage at
    # Recommend the lowest threshold reaching this precision; --target-precision overrides it.
    target_precision: float | None = Field(default=None, ge=0, le=1)


class EvalConfig(BaseModel):
    # Unknown top-level keys are kept (``model_extra``) so other slices can add
    # sections without a schema change here.
    model_config = ConfigDict(extra="allow")

    golden: str  # golden JSONL, relative to the config file
    decide: str | None = None  # "file.py:func" turning a row's replies into a Decision
    reference: str | None = None  # defaults to the first candidate
    noise_replays: int = Field(default=2, ge=1)  # times the reference is replayed (noise floor)
    budget_usd: float | None = Field(default=None, ge=0)  # stop before spending more than this
    seed: int = 0  # bootstrap and sampling seed
    # [[candidates]]; the first one is the reference unless `reference` says otherwise.
    candidates: list[Candidate] = Field(min_length=1)
    pricing: dict[str, Price] = Field(default_factory=dict)  # model id -> price override
    costs: dict[str, float] = Field(default_factory=dict)  # "from->to" -> cost of that change
    ci: CIConfig = Field(default_factory=CIConfig)  # [ci] gate settings
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)  # [calibration]
    operating_point: OperatingPointConfig | None = None  # [operating_point]
    cost_projection: CostProjectionConfig | None = None  # [cost_projection], for $/month
    slices: list[SliceConfig] = Field(default_factory=list)  # [[slices]] with their own rules
    quality: QualityConfig | None = None  # [quality]: human approvals, to measure "better"
    config_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> EvalConfig:
        names = [c.name for c in self.candidates]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate candidate names: {dupes}")
        if self.reference is None:
            self.reference = names[0]
        elif self.reference not in names:
            raise ValueError(f"reference {self.reference!r} is not a candidate name {names}")
        slice_names = [s.name for s in self.slices]
        dupes = sorted({n for n in slice_names if slice_names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate slice names: {dupes}")
        label_rules = {SliceRule.MUST_NOT_LOSE, SliceRule.MIN_PRECISION}
        needs_op = [s.name for s in self.slices if s.rule in label_rules]
        if needs_op and self.operating_point is None:
            raise ValueError(
                f"slices {needs_op} use must_not_lose/min_precision, which need "
                "[operating_point] auto_outcomes"
            )
        if self.ci.quality_per_cost is not None and self.quality is None:
            raise ValueError(
                "[ci] quality_per_cost needs a [quality] section (approvals and/or "
                "golden_labels) to measure quality against"
            )
        for key in self.costs:
            if "->" not in key:
                raise ValueError(f"costs key {key!r} must look like 'from->to'")
        return self

    # ------------------------------------------------------------------ helpers

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.config_dir / p)

    @property
    def golden_path(self) -> Path:
        return self.resolve(self.golden)

    @property
    def reference_candidate(self) -> Candidate:
        return self.candidate(self.reference or self.candidates[0].name)

    def candidate(self, name: str) -> Candidate:
        for c in self.candidates:
            if c.name == name:
                return c
        raise KeyError(name)

    def replicas_for(self, candidate: str) -> int:
        """Reference is replayed ``noise_replays`` times; others once."""
        return self.noise_replays if candidate == self.reference else 1

    def load_decide(self) -> DecideFn:
        from caliper_eval.loader import load_callable

        if not self.decide:
            raise ValueError("config has no 'decide' (expected 'file.py:func')")
        return load_callable(self.decide, self.config_dir)

    def transition_cost(self, from_outcome: str, to_outcome: str) -> float:
        """Cost weight of a changed outcome; 0 when unchanged, default 1.0."""
        if from_outcome == to_outcome:
            return 0.0
        return self.costs.get(f"{from_outcome}->{to_outcome}", 1.0)

    def fingerprint(self) -> str:
        """Stable hash of the config content (not its location)."""
        dumped = self.model_dump(mode="json")
        return hashlib.sha256(canonical_json(dumped).encode()).hexdigest()


def config_from_dict(data: dict[str, Any], config_dir: str | Path = ".") -> EvalConfig:
    return EvalConfig.model_validate({**data, "config_dir": Path(config_dir)})


def load_config(path: str | Path) -> EvalConfig:
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        return config_from_dict(data, path.resolve().parent)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
