"""Inter-rater kappa from ``LabelRecord`` lists (what ``caliper label`` writes).

Thin adapter over :func:`caliper_kappa.inter_rater_kappa`, which stays free of
any dependency on caliper_eval. This module owns the ``LabelRecord``
semantics, applied in this order:

1. **Undo.** A record with ``undoes=X`` retracts the record whose
   ``record_id`` is ``X``. Both drop out.
2. **Source filter.** With ``sources`` set, only records from those
   :class:`LabelSource` values are kept. The default keeps human sources
   only (``human_end_user``, ``internal_reviewer``): agreement with an LLM
   teacher is "teacher agreement", not inter-rater agreement.
3. **Skips.** ``skipped`` records and records with ``decision is None`` drop.
4. **Latest per (item, labeller).** When a labeller labelled the same item
   twice, the record with the greatest ``labelled_at`` wins (ISO-8601 UTC
   strings compare correctly as text); ties go to the later record in input
   order.

The category of an item is its record's ``category`` (first non-null wins;
conflicts are reported).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from caliper_kappa import InterRaterReport, RaterLabel, inter_rater_kappa
from pydantic import ValidationError

from caliper_eval.types import LabelRecord, LabelSource

HUMAN_SOURCES: frozenset[LabelSource] = frozenset(s for s in LabelSource if s.is_human)


def resolve_label_records(
    records: Iterable[LabelRecord],
    *,
    sources: Iterable[LabelSource | str] | None = None,
) -> list[LabelRecord]:
    """Apply undo, source filter, skip and latest-per-labeller rules (see module doc).

    Returns the surviving records sorted by ``(item_id, labeller)``.
    ``sources=None`` means human sources only; pass ``list(LabelSource)`` for all.
    """
    recs = list(records)
    undone = {r.undoes for r in recs if r.undoes}
    keep_sources = (
        HUMAN_SOURCES if sources is None else frozenset(LabelSource(s) for s in sources)
    )
    latest: dict[tuple[str, str], tuple[str, int, LabelRecord]] = {}
    for idx, r in enumerate(recs):
        if r.undoes or r.record_id in undone:
            continue
        if r.source not in keep_sources:
            continue
        if r.skipped or r.decision is None:
            continue
        key = (r.item_id, r.labeller)
        cand = (r.labelled_at, idx, r)
        prev = latest.get(key)
        if prev is None or (cand[0], cand[1]) >= (prev[0], prev[1]):
            latest[key] = cand
    return [latest[k][2] for k in sorted(latest)]


def inter_rater_from_records(
    records: Iterable[LabelRecord],
    *,
    sources: Iterable[LabelSource | str] | None = None,
    method: Literal["auto", "cohen", "fleiss"] = "auto",
) -> InterRaterReport:
    """Inter-rater kappa (overall + by category) straight from ``LabelRecord`` s."""
    resolved = resolve_label_records(records, sources=sources)
    return inter_rater_kappa(
        [
            RaterLabel(
                item_id=r.item_id,
                rater=r.labeller,
                label=str(r.decision),
                category=r.category,
            )
            for r in resolved
        ],
        method=method,
    )


def load_label_records(path: str | Path) -> list[LabelRecord]:
    """Read a ``LabelRecord`` JSONL file (blank lines ignored)."""
    out: list[LabelRecord] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not JSON: {exc}") from exc
            try:
                out.append(LabelRecord.model_validate(obj))
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                    for e in exc.errors()[:4]
                )
                hint = ""
                if isinstance(obj, dict) and "rollout" in obj:
                    hint = " (this looks like a labeled-rollouts file: use --rollouts)"
                raise ValueError(f"{path}:{lineno}: not a LabelRecord{hint}: {problems}") from exc
    return out


__all__ = [
    "HUMAN_SOURCES",
    "inter_rater_from_records",
    "load_label_records",
    "resolve_label_records",
]
