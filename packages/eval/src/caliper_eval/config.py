"""Eval configuration, loaded from TOML.

Relative paths (``golden``, ``decide``, candidates' ``fake_script``) resolve
against the directory holding the config file (``EvalConfig.config_dir``).
They are kept relative in the model itself so nothing machine-specific leaks
into results or reports.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from caliper_eval.types import Candidate, DecideFn, Price, canonical_json


class CIConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_regression: float = 0.0


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
