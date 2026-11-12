"""Human approvals of outcomes: the evidence behind "better", not just "different".

An **approval** says whether a person would accept one *outcome* for one golden
row. It is keyed by ``(row_id, outcome)``, never by candidate, so:

* the reviewer is blind to which model produced the outcome (``caliper review``
  shows the request and the outcome only);
* one approval serves every candidate that reached that outcome on that row,
  including candidates added later;
* when two candidates decide the same, they are both right or both wrong, so
  only rows where they **differ** need review to compare them.

Sources of verdicts, in ``[quality]``:

* ``approvals``: JSONL files. Each line is either the native shape
  ``{"row_id", "outcome", "approved", "source", "request_sha256"?, "reviewer"?}``
  or a ``caliper label export --format jsonl`` row of a ``caliper review``
  project (``item.row_id``, ``item.outcome``, ``item.request_sha256``,
  ``approved``, ``source``, ``labeller``).
* ``golden_labels`` (default true): a golden row with a human ``label`` approves
  that outcome and rejects every other outcome on the row.

Only human sources count (``human_end_user``, ``internal_reviewer``,
``adjudicated``). An ``llm_teacher`` or ``heuristic`` approval is ignored and
counted, because agreement with a model is not evidence of being right.
``approved: null`` (an "Unsure" answer) gives no verdict. An approval whose
``request_sha256`` no longer matches the golden row is stale and ignored.

For one ``(row_id, outcome)``, adjudicated lines win over other lines, and any
approval line wins over the golden label. Lines that contradict each other at
the level that decides give no verdict and are counted as conflicts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from ._stats import transition_cost
from .types import GoldenRow, LabelSource, canonical_json, request_sha256


class Approval(BaseModel):
    """One person's verdict on one outcome for one golden row."""

    model_config = ConfigDict(extra="ignore")

    row_id: str  # golden row id
    outcome: str  # the decided outcome being judged
    approved: bool | None  # true: a person would accept it; null: unsure (no verdict)
    source: LabelSource = LabelSource.INTERNAL_REVIEWER  # who judged it; only human sources count
    request_sha256: str | None = None  # the request the reviewer saw; stale when it changed
    reviewer: str | None = None  # free-form reviewer name


class ApprovalSummary(BaseModel):
    """What the approval evidence contained and what was set aside."""

    model_config = ConfigDict(extra="forbid")

    files: list[str]  # approval files read, as given in [quality] approvals
    golden_labels: bool  # whether human golden labels count as verdicts
    used: int  # human verdicts in use (approval lines, before merging duplicates)
    from_golden_labels: int  # golden rows whose human label gives verdicts
    labels_not_outcomes: int = 0  # human golden labels that are no decided outcome (ignored)
    overrides: int = 0  # approval verdicts that differ from the row's golden label (they win)
    not_human: int  # lines from a non-human source (ignored)
    unsure: int  # lines with approved = null (ignored)
    stale: int  # lines for a request that has changed since (ignored)
    unknown_rows: int  # lines for a row id not in the golden set (ignored)
    conflicts: int  # (row, outcome) pairs with contradicting verdicts (no verdict)
    fingerprint: str  # hash of every verdict in use (part of the test-ledger key)


class Verdicts:
    """Lookup ``(row_id, outcome) -> approved?``; ``None`` when no person has said."""

    def __init__(
        self,
        explicit: dict[tuple[str, str], bool],
        labels: dict[str, str],
        void: set[tuple[str, str]] | None = None,
    ) -> None:
        self._explicit = explicit
        self._labels = labels
        self._void = void or set()

    def get(self, row_id: str, outcome: str) -> bool | None:
        if (row_id, outcome) in self._void:
            return None
        v = self._explicit.get((row_id, outcome))
        if v is not None:
            return v
        label = self._labels.get(row_id)
        if label is None:
            return None
        return outcome == label


def _line_to_approval(obj: dict) -> dict:
    """Normalise a ``caliper label export --format jsonl`` row to the native shape."""
    item = obj.get("item")
    if not isinstance(item, dict) or "row_id" in obj:
        return obj
    return {
        "row_id": item.get("row_id"),
        "outcome": item.get("outcome"),
        "request_sha256": item.get("request_sha256"),
        "approved": obj.get("approved"),
        "source": obj.get("source") or LabelSource.INTERNAL_REVIEWER.value,
        "reviewer": obj.get("labeller"),
    }


def read_approvals(path: str | Path) -> list[Approval]:
    """Approvals from one JSONL file. Raises ``ValueError`` naming the bad line."""
    p = Path(path)
    out: list[Approval] = []
    with p.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
                out.append(Approval.model_validate(_line_to_approval(obj)))
            except ValidationError as exc:
                why = "; ".join(
                    f"{'.'.join(str(x) for x in e['loc']) or 'line'}: {e['msg']}"
                    for e in exc.errors()
                )
                raise ValueError(f"{p}:{n}: invalid approval: {why}") from exc
            except ValueError as exc:
                raise ValueError(f"{p}:{n}: invalid approval: {exc}") from exc
    return out


def build_verdicts(
    golden: Sequence[GoldenRow],
    approvals: Iterable[Approval],
    *,
    files: Sequence[str] = (),
    golden_labels: bool = True,
    outcomes: Iterable[str] | None = None,
) -> tuple[Verdicts, ApprovalSummary]:
    """Merge approval lines and human golden labels into one verdict lookup.

    Precedence for one ``(row, outcome)``: adjudicated approval lines, then other
    approval lines, then the row's golden label. Lines that disagree at the level
    that decides give no verdict (a conflict). ``outcomes`` are the outcomes any
    candidate decided: a golden label that is none of them (e.g. "yes" from a
    yes/no project) cannot judge an outcome and is ignored.
    """
    by_id = {g.id: g for g in golden}
    want_sha = {g.id: request_sha256(g.request) for g in golden}
    not_human = unsure = stale = unknown = used = 0
    seen: dict[tuple[str, str], dict[bool, set[bool]]] = {}
    for a in approvals:
        if a.row_id not in by_id:
            unknown += 1
            continue
        if not a.source.is_human:
            not_human += 1
            continue
        if a.approved is None:
            unsure += 1
            continue
        if a.request_sha256 is not None and a.request_sha256 != want_sha[a.row_id]:
            stale += 1
            continue
        used += 1
        by_level = seen.setdefault((a.row_id, a.outcome), {})
        by_level.setdefault(a.source == LabelSource.ADJUDICATED, set()).add(a.approved)

    known = None if outcomes is None else set(outcomes)
    labels: dict[str, str] = {}
    not_an_outcome = 0
    if golden_labels:
        for g in golden:
            if g.label is None or g.label_source is None or not g.label_source.is_human:
                continue
            if known is not None and g.label not in known:
                not_an_outcome += 1
                continue
            labels[g.id] = g.label

    explicit: dict[tuple[str, str], bool] = {}
    conflicts: set[tuple[str, str]] = set()
    overrides = 0
    for key, by_level in seen.items():
        values = by_level.get(True) or by_level[False]
        if len(values) > 1:
            conflicts.add(key)
            continue
        value = next(iter(values))
        explicit[key] = value
        label = labels.get(key[0])
        if label is not None and value != (key[1] == label):
            overrides += 1

    # Every verdict in use enters the fingerprint, which keys the test ledger: new
    # verdicts change gated numbers, so the run after a review is a new measurement.
    h = hashlib.sha256()
    for key in sorted(explicit):
        h.update(canonical_json([key[0], key[1], explicit[key]]).encode("utf-8"))
    for row_id in sorted(labels):
        h.update(canonical_json([row_id, "label", labels[row_id]]).encode("utf-8"))
    for key in sorted(conflicts):
        h.update(canonical_json([key[0], key[1], None]).encode("utf-8"))
    # A conflict voids the pair, including what a golden label would say about it.
    verdicts = Verdicts(explicit, labels, conflicts)
    summary = ApprovalSummary(
        files=list(files),
        golden_labels=golden_labels,
        used=used,
        from_golden_labels=len(labels),
        labels_not_outcomes=not_an_outcome,
        overrides=overrides,
        not_human=not_human,
        unsure=unsure,
        stale=stale,
        unknown_rows=unknown,
        conflicts=len(conflicts),
        fingerprint=h.hexdigest(),
    )
    return verdicts, summary


def judged_cost(
    costs: Mapping[str, float], verdicts: Verdicts | None, row_id: str, src: str, dst: str
) -> float:
    """Cost of the change ``src -> dst`` on a row; 0 when people call it a fix.

    A fix is a change from an outcome a person rejected to one a person approved.
    A change between two acceptable outcomes still costs its ``[costs]`` weight:
    "acceptable" is not "free" (an extra review is still an extra review).
    """
    if (
        src != dst
        and verdicts is not None
        and verdicts.get(row_id, src) is False
        and verdicts.get(row_id, dst) is True
    ):
        return 0.0
    return transition_cost(costs, src, dst)


__all__ = [
    "Approval",
    "ApprovalSummary",
    "Verdicts",
    "build_verdicts",
    "judged_cost",
    "read_approvals",
]
