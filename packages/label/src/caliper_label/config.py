"""`label.yaml` / `label.toml`: what a card shows and which keys decide.

Example::

    title: Catalog cleanup review
    fields:
      - {key: request.messages.-1.content, type: text, label: Listing}
      - {key: metadata.production_tax_code, type: badge, label: Code}
      - {key: metadata.production_score, type: number, decimals: 2}
      - {template: "cards/{id}.svg", type: image, label: Photo}
    decisions:
      - {key: "1", value: correct, label: Code correct, approved: true}
      - {key: "2", value: wrong, label: Wrong code}
    queue_key: stratum
    category_key: metadata.production_tax_code
    double_label: 1

Paths are dotted (``metadata.supplier``); integer parts index lists and may be
negative (``request.messages.-1.content`` is the last message).
"""

from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from caliper_eval.types import LabelSource
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Keys the UI binds itself; decisions may not use them.
RESERVED_KEYS = {"z", "?", "escape", "enter", "tab", " "}

_SPECIAL_KEYS = {"backspace", "delete", "escape", "enter", "tab", "space"}


class FieldType(StrEnum):
    TEXT = "text"
    LINK = "link"
    NUMBER = "number"
    BADGE = "badge"
    IMAGE = "image"


class FieldSpec(BaseModel):
    """One thing shown on the card. Give either ``key`` (a dotted path) or ``template``."""

    model_config = ConfigDict(extra="forbid")

    type: FieldType = FieldType.TEXT
    key: str | None = None
    template: str | None = None  # "cards/{id}.svg": {dotted.path} placeholders
    label: str | None = None
    decimals: int | None = Field(default=None, ge=0, le=10)  # number fields
    mono: bool = False  # text fields: monospace, keep whitespace

    @model_validator(mode="after")
    def _one_source(self) -> FieldSpec:
        if (self.key is None) == (self.template is None):
            raise ValueError("a field needs exactly one of 'key' or 'template'")
        return self

    @property
    def name(self) -> str:
        return self.label or self.key or self.template or self.type.value


class DecisionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str  # single key, e.g. "1" or "t"
    value: str  # stored in LabelRecord.decision
    label: str | None = None  # shown on the button
    approved: bool = False  # the human approved the output (GoldenRow.label_approved)
    score: float | None = Field(default=None, ge=0.0, le=1.0)  # human_label; default 1/0
    outcome: str | None = None  # GoldenRow.label on export; default: value

    @field_validator("key", mode="before")
    @classmethod
    def _key(cls, v: Any) -> str:
        return _norm_key(str(v))

    @property
    def human_label(self) -> float:
        if self.score is not None:
            return self.score
        return 1.0 if self.approved else 0.0


class Keys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip: str = "s"
    undo: str = "backspace"
    note: str = "n"

    @field_validator("skip", "undo", "note", mode="before")
    @classmethod
    def _key(cls, v: Any) -> str:
        return _norm_key(str(v))


class RolloutSpec(BaseModel):
    """How a non-EvalRow row becomes a Rollout on ``export --format rollouts``.

    Each is a dotted path. Defaults: prompt = the request rendered as text,
    response = ``reference_outcome``, gold = none.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    response: str | None = None
    gold: str | None = None


class LabelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "caliper label"
    instructions: str | None = None
    id_key: str = "id"
    fields: list[FieldSpec] = Field(min_length=1)
    decisions: list[DecisionSpec] = Field(min_length=1)
    approved: list[str] = Field(default_factory=list)  # alternative to decision.approved
    keys: Keys = Field(default_factory=Keys)
    queue_key: str | None = None
    category_key: str | None = None
    source: LabelSource = LabelSource.INTERNAL_REVIEWER
    stage: str | None = None
    double_label: int = Field(default=1, ge=1, le=10)  # labels needed per item
    image_root: str | None = None  # local images resolve here; default: the data file's dir
    rollout: RolloutSpec = Field(default_factory=RolloutSpec)

    @model_validator(mode="after")
    def _check(self) -> LabelConfig:
        values = [d.value for d in self.decisions]
        if len(set(values)) != len(values):
            raise ValueError(f"decision values must be unique: {values}")
        unknown = [a for a in self.approved if a not in values]
        if unknown:
            raise ValueError(f"approved lists unknown decision value(s) {unknown}")
        for d in self.decisions:
            if d.value in self.approved:
                d.approved = True
        used: dict[str, str] = {}
        bindings = [(d.key, f"decision '{d.value}'") for d in self.decisions] + [
            (self.keys.skip, "skip"),
            (self.keys.undo, "undo"),
            (self.keys.note, "note"),
        ]
        for key, what in bindings:
            if key in RESERVED_KEYS:
                raise ValueError(f"key '{key}' for {what} is reserved (z, ?, Esc, Enter, Tab)")
            if key in used:
                raise ValueError(f"key '{key}' is bound twice: {used[key]} and {what}")
            used[key] = what
        return self

    def decision(self, value: str) -> DecisionSpec | None:
        for d in self.decisions:
            if d.value == value:
                return d
        return None


def _norm_key(key: str) -> str:
    """Keys are compared to ``KeyboardEvent.key`` lowercased; names like 'Backspace' allowed."""
    if len(key) == 1:
        return key.lower()
    low = key.strip().lower()
    if low == "esc":
        low = "escape"
    if low not in _SPECIAL_KEYS:
        raise ValueError(f"key '{key}' must be one character or one of {sorted(_SPECIAL_KEYS)}")
    return " " if low == "space" else low


def load_label_config(path: str | Path) -> LabelConfig:
    """Load a YAML (``.yaml``/``.yml``) or TOML (``.toml``) label config."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() == ".toml":
            raw = tomllib.loads(text)
        else:
            raw = yaml.safe_load(text)
    except (tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    try:
        return LabelConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: {_short_errors(exc)}") from exc


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


# --------------------------------------------------------------------------- paths

_MISSING = object()
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """Resolve ``a.b.0.c`` (negative ints index from the end) on dicts and lists."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                cur = _MISSING
        else:
            cur = _MISSING
        if cur is _MISSING:
            return default
    return cur


def render_template(template: str, row: dict[str, Any]) -> str | None:
    """Fill ``{dotted.path}`` placeholders; None if any placeholder is missing."""
    missing = False

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        val = get_path(row, m.group(1))
        if val is None:
            missing = True
            return ""
        return str(val)

    out = _PLACEHOLDER.sub(sub, template)
    return None if missing else out
