"""`caliper.label.yaml` v1: the project config (CONTRACT - see packages/label/CONTRACT.md).

The UI is the main way to write this file; hand-editing stays supported and the
two directions must round-trip losslessly (owned by dev 2: load/save/migrate).
Credentials never appear here: connections only *reference* env vars, AWS
profiles, or the default credential chain.

Example::

    version: 1
    name: Support ticket triage
    source:
      kind: file
      path: data/tickets.csv          # relative to the project dir
      id_column: ticket_id
    card:
      fields:
        - {column: subject, type: text}
        - {column: body, type: long_text}
        - {column: screenshot_url, type: image}
    labels:
      type: single
      options:
        - {value: correct, label: Correct, hotkey: "1", color: "#2e7d32", approved: true}
        - {value: wrong, label: Wrong, hotkey: "2", color: "#c62828"}
        - {value: unsure, label: Unsure, hotkey: "3", requires_note: true}
    workflow:
      queues: {by: priority}
      double_label: 2
      assignment: round_robin
      instructions: |
        Is the triage label right for this ticket?
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from caliper_eval.types import LabelSource
from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME = "caliper.label.yaml"
STATE_DIRNAME = ".caliper-label"  # project store, snapshots, caches; gitignored


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- source


class SourceKind(StrEnum):
    FILE = "file"  # csv/tsv/jsonl/json/parquet/xlsx, local path or uploaded copy
    FOLDER = "folder"  # folder of images -> one item per image (+ optional sidecar)
    S3 = "s3"  # s3:// incl. S3-compatible endpoints (R2, MinIO)
    GCS = "gcs"
    HTTP = "http"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    PLUGIN = "plugin"  # third-party connector registered via entry point


class Credentials(_M):
    """References only. Never a secret value."""

    env: str | None = None  # e.g. "DATABASE_URL" (whole DSN) or "PGPASSWORD"
    env_map: dict[str, str] = Field(default_factory=dict)  # option -> ENV_VAR
    aws_profile: str | None = None  # else default chain (env, SSO, instance role)
    region: str | None = None
    endpoint_url: str | None = None  # S3-compatible endpoints (not a secret)


class SourceConfig(_M):
    kind: SourceKind
    path: str | None = None  # file/folder/sqlite path, or URL / glob for s3/gcs/http
    format: str | None = None  # csv|tsv|jsonl|json|parquet|xlsx; None = by extension
    table: str | None = None  # databases: a table ...
    query: str | None = None  # ... or a SQL query (exactly one of table/query)
    sidecar: str | None = None  # folder sources: CSV joined on file name
    id_column: str | None = None  # None = row number (stable within a snapshot)
    plugin: str | None = None  # kind=plugin: entry-point name
    options: dict[str, Any] = Field(default_factory=dict)  # connector-specific, non-secret
    credentials: Credentials = Field(default_factory=Credentials)


# --------------------------------------------------------------------------- card


class DisplayType(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    NUMBER = "number"
    BADGE = "badge"
    LINK = "link"
    IMAGE = "image"  # URL, s3:// key, or local path (served safely / presigned)
    JSON = "json"


class CardField(_M):
    column: str | None = None  # source column; dotted paths into JSON columns allowed
    template: str | None = None  # "cards/{id}.svg" style; exactly one of column/template
    type: DisplayType = DisplayType.TEXT
    label: str | None = None
    decimals: int | None = Field(default=None, ge=0, le=10)
    hidden: bool = False  # kept in snapshot/export but not shown


class CardConfig(_M):
    fields: list[CardField] = Field(default_factory=list)


# --------------------------------------------------------------------------- labels


class LabelType(StrEnum):
    SINGLE = "single"
    MULTI = "multi"
    YES_NO_UNSURE = "yes_no_unsure"  # preset of SINGLE
    FREE_TEXT = "free_text"
    SCORE = "score"


class LabelOption(_M):
    value: str  # stored value; stable identity (renames change `label`, or migrate)
    label: str | None = None
    hotkey: str | None = None  # auto-assigned 1..9 by the wizard; clash-checked
    color: str | None = None  # "#rrggbb"
    approved: bool = False  # counts as approved/positive (label_approved, human_label=1)
    requires_note: bool = False
    outcome: str | None = None  # GoldenRow.label on export; default: value
    score: float | None = Field(default=None, ge=0.0, le=1.0)  # human_label override
    retired: bool = False  # removed in settings after labels existed: kept, flagged


class ScoreRange(_M):
    min: float = 1
    max: float = 5
    step: float = 1


class Keys(_M):
    skip: str = "s"
    undo: str = "backspace"
    note: str = "n"


class LabelsConfig(_M):
    type: LabelType = LabelType.SINGLE
    options: list[LabelOption] = Field(default_factory=list)
    score: ScoreRange | None = None  # type=score
    keys: Keys = Field(default_factory=Keys)
    category_column: str | None = None  # inter-annotator kappa by category


# --------------------------------------------------------------------------- workflow


class Assignment(StrEnum):
    OPEN = "open"
    ROUND_ROBIN = "round_robin"


class QueueConfig(_M):
    by: str | None = None  # column (or "stratum" from `caliper sample`)


class GoldenCheck(_M):
    """Hidden items with known answers; per-labeller accuracy on them."""

    answer_column: str  # rows with a non-empty value here are golden items
    rate: float = Field(default=0.05, ge=0.0, le=1.0)  # share of served items


class WorkflowConfig(_M):
    queues: QueueConfig = Field(default_factory=QueueConfig)
    double_label: int = Field(default=1, ge=1, le=10)
    assignment: Assignment = Assignment.OPEN
    instructions: str | None = None  # markdown
    golden: GoldenCheck | None = None
    source: LabelSource = LabelSource.INTERNAL_REVIEWER
    stage: str | None = None


# --------------------------------------------------------------------------- export


class ExportTarget(_M):
    """A saved export destination (optional). Same connector/credential rules as source."""

    name: str
    format: str  # golden|rollouts|records|csv|jsonl|parquet|table
    destination: SourceConfig  # file path / s3 URL / database table


# --------------------------------------------------------------------------- project


class ProjectConfig(_M):
    version: int = 1
    name: str = "Untitled project"
    description: str | None = None
    source: SourceConfig
    card: CardConfig = Field(default_factory=CardConfig)
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    exports: list[ExportTarget] = Field(default_factory=list)
