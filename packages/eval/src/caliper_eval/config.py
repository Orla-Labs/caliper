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

    max_regression: float = 0.0
    require: GateRequirement = GateRequirement.NON_INFERIORITY


class OperatingPointConfig(BaseModel):
    """Matched-operating-point comparison (``[operating_point]``).

    ``auto_outcomes`` are the outcomes that count as "handled automatically"
    (auto-published, expensive call skipped, ...). The reference's share of
    those on val is the coverage the candidate's score threshold is matched to.
    """

    model_config = ConfigDict(extra="forbid")

    auto_outcomes: list[str] = Field(min_length=1)


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

    name: str
    rule: SliceRule
    threshold: float | None = None
    where: dict[str, Any] = Field(default_factory=dict)
    label_sources: list[LabelSource] | None = None

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


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    thresholds: list[float] | None = None


class EvalConfig(BaseModel):
    # Unknown top-level keys are kept (``model_extra``) so other slices can add
    # sections without a schema change here.
    model_config = ConfigDict(extra="allow")

    golden: str
    decide: str | None = None
    reference: str | None = None  # defaults to the first candidate
    noise_replays: int = Field(default=2, ge=1)
    budget_usd: float | None = Field(default=None, ge=0)
    seed: int = 0
    candidates: list[Candidate] = Field(min_length=1)
    pricing: dict[str, Price] = Field(default_factory=dict)
    costs: dict[str, float] = Field(default_factory=dict)
    ci: CIConfig = Field(default_factory=CIConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    operating_point: OperatingPointConfig | None = None
    slices: list[SliceConfig] = Field(default_factory=list)
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
