"""Label provenance and selection splits: who produced a label, and which rows
a threshold may be picked on.

Every label-based metric (calibration sweep, kappa, matched operating point,
slice precision) is reported **per label source**. The source decides what a
number may be called:

* human sources (``human_end_user``, ``internal_reviewer``) are truth, so the
  share of auto-handled rows a human approved is **precision**;
* ``llm_teacher`` labels give **teacher agreement**, never accuracy or precision;
* ``heuristic`` labels give **heuristic agreement**;
* rows with no ``label_source`` are ``unknown`` and give **agreement
  (unknown source)**.

Splits (``GoldenRow.split``): when any row carries a split, thresholds are
picked on ``val`` rows only and reported on ``test`` rows. When no row carries
one, selection and reporting use every row (the pre-split behaviour), and the
report says so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .types import GoldenRow, LabelSource, Split

UNKNOWN = "unknown"
HUMAN_KEYS = frozenset(s.value for s in LabelSource if s.is_human)

# kind -> (metric name, short kind label)
_KINDS = {
    "human_truth": "precision",
    "teacher_agreement": "teacher agreement",
    "heuristic_agreement": "heuristic agreement",
    "unknown_source": "agreement (unknown source)",
    "mixed_labels": "label agreement",
}


def source_key(row: GoldenRow) -> str:
    """The row's label source as a string; ``"unknown"`` when not recorded."""
    # str() of a LabelSource is its value; also tolerates a plain string assigned
    # after validation.
    return str(row.label_source) if row.label_source is not None else UNKNOWN


def is_human(key: str) -> bool:
    return key in HUMAN_KEYS


def basis_kind(keys: Iterable[str]) -> str:
    """What agreement with labels from ``keys`` is: truth or something weaker."""
    ks = set(keys)
    if ks and ks <= HUMAN_KEYS:
        return "human_truth"
    if ks == {LabelSource.LLM_TEACHER.value}:
        return "teacher_agreement"
    if ks == {LabelSource.HEURISTIC.value}:
        return "heuristic_agreement"
    if ks == {UNKNOWN}:
        return "unknown_source"
    return "mixed_labels"


def metric_name(keys: Iterable[str]) -> str:
    """'precision' only for human truth; otherwise the honest agreement name."""
    return _KINDS[basis_kind(keys)]


def has_splits(golden: Iterable[GoldenRow]) -> bool:
    return any(g.split is not None for g in golden)


def selection_rows(golden: Iterable[GoldenRow]) -> tuple[list[GoldenRow], str]:
    """Rows a threshold may be picked on, and the split name ('val' or 'all')."""
    golden = list(golden)
    if has_splits(golden):
        return [g for g in golden if g.split == Split.VAL], Split.VAL.value
    return golden, "all"


def report_rows(golden: Iterable[GoldenRow]) -> tuple[list[GoldenRow], str]:
    """Rows selected thresholds are reported on, and the split name ('test' or 'all')."""
    golden = list(golden)
    if has_splits(golden):
        return [g for g in golden if g.split == Split.TEST], Split.TEST.value
    return golden, "all"


class ProvenanceSummary(BaseModel):
    """Where the golden set's labels come from, and how it is split."""

    model_config = ConfigDict(extra="forbid")

    n_labelled: int  # rows with label_approved
    by_source: dict[str, int] = Field(default_factory=dict)  # labelled rows per source key
    by_split: dict[str, int] = Field(default_factory=dict)  # all rows per split ("none")
    n_human_truth: int = 0
    has_human_truth: bool = False
    has_splits: bool = False


def summarize(golden: Iterable[GoldenRow]) -> ProvenanceSummary:
    golden = list(golden)
    labelled = [g for g in golden if g.label_approved is not None]
    by_source = Counter(source_key(g) for g in labelled)
    by_split = Counter(g.split.value if g.split is not None else "none" for g in golden)
    n_human = sum(n for k, n in by_source.items() if is_human(k))
    return ProvenanceSummary(
        n_labelled=len(labelled),
        by_source=dict(sorted(by_source.items())),
        by_split=dict(sorted(by_split.items())),
        n_human_truth=n_human,
        has_human_truth=n_human > 0,
        has_splits=has_splits(golden),
    )


__all__ = [
    "HUMAN_KEYS",
    "UNKNOWN",
    "ProvenanceSummary",
    "basis_kind",
    "has_splits",
    "is_human",
    "metric_name",
    "report_rows",
    "selection_rows",
    "source_key",
    "summarize",
]
