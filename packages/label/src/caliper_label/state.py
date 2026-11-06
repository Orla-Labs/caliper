"""Labelling state folded from the append-only log, plus the write operations.

A record is *active* when no later undo record retracts it. For each
(item, labeller) the latest active record wins: a decision is a label, a skip
means "not now". Undo records are never active themselves.

An item is *complete* when ``double_label`` distinct labellers have an active
decision on it. A labeller is never served an item they already labelled. While
a card is on someone's screen it holds a short lease, so two labellers working
the same queue are not shown the same item when it only needs one more label.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from caliper_eval.types import LabelRecord

from .config import LabelConfig
from .data import Dataset, Item
from .store import LabelStore, new_record_id, utc_now

LEASE_SECONDS = 600.0


class LabelError(ValueError):
    """A request the state cannot apply (unknown item/decision, already labelled)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ItemState:
    labels: dict[str, LabelRecord]  # labeller -> active decision record
    skips: dict[str, LabelRecord]  # labeller -> active skip record (no later label)


def fold(records: list[LabelRecord]) -> tuple[dict[str, ItemState], list[LabelRecord]]:
    """Return per-item state and the active label/skip records in log order."""
    undone = {r.undoes for r in records if r.undoes}
    active = [r for r in records if not r.undoes and r.record_id not in undone]
    states: dict[str, ItemState] = {}
    for r in active:
        st = states.setdefault(r.item_id, ItemState({}, {}))
        if r.skipped or r.decision is None:
            st.skips[r.labeller] = r
            st.labels.pop(r.labeller, None)
        else:
            st.labels[r.labeller] = r
            st.skips.pop(r.labeller, None)
    return states, active


class Session:
    """Thread-safe labelling session over one dataset and one store."""

    def __init__(self, dataset: Dataset, config: LabelConfig, store: LabelStore) -> None:
        self.dataset = dataset
        self.config = config
        self.store = store
        self._lock = threading.RLock()
        self.records: list[LabelRecord] = store.load()
        unknown = {r.item_id for r in self.records} - set(dataset.by_id)
        self.unknown_items = sorted(unknown)  # labels for rows not in this data file
        self._refold()
        self._leases: dict[str, tuple[str, float]] = {}  # labeller -> (item_id, expires)

    # ------------------------------------------------------------------ folding

    def _refold(self) -> None:
        self.states, self.active = fold(self.records)

    def state(self, item_id: str) -> ItemState:
        return self.states.get(item_id) or ItemState({}, {})

    @property
    def needed(self) -> int:
        return self.config.double_label

    def is_complete(self, item_id: str) -> bool:
        return len(self.state(item_id).labels) >= self.needed

    # ------------------------------------------------------------------ serving

    def _leased_by_others(self, item_id: str, labeller: str, now: float) -> int:
        return sum(
            1
            for who, (iid, exp) in self._leases.items()
            if iid == item_id and who != labeller and exp > now
        )

    def _eligible(self, item: Item, labeller: str, now: float) -> bool:
        st = self.state(item.id)
        if labeller in st.labels:
            return False
        others = len(st.labels) + self._leased_by_others(item.id, labeller, now)
        return others < self.needed

    def next_item(self, labeller: str, queue: str | None = None) -> Item | None:
        """Next item this labeller may label, in data order.

        With double labelling, items someone else already labelled come first, so
        the overlap set (and the agreement numbers) fill up early. Items this
        labeller skipped come last.
        """
        with self._lock:
            now = time.monotonic()
            started: Item | None = None  # someone else labelled it: finish the overlap first
            fresh: Item | None = None
            skipped_fallback: Item | None = None
            for item in self.dataset.items:
                if queue and item.queue != queue:
                    continue
                if not self._eligible(item, labeller, now):
                    continue
                st = self.state(item.id)
                if labeller in st.skips:
                    skipped_fallback = skipped_fallback or item
                elif st.labels:
                    started = item
                    break
                elif fresh is None:
                    fresh = item
                    if self.needed == 1:
                        break
            chosen = started or fresh or skipped_fallback
            if chosen is not None:
                self._leases[labeller] = (chosen.id, now + LEASE_SECONDS)
            else:
                self._leases.pop(labeller, None)
            return chosen

    def hold(self, labeller: str, item_id: str) -> None:
        with self._lock:
            self._leases[labeller] = (item_id, time.monotonic() + LEASE_SECONDS)

    # ------------------------------------------------------------------ writing

    def _item(self, item_id: str) -> Item:
        item = self.dataset.by_id.get(item_id)
        if item is None:
            raise LabelError(f"unknown item '{item_id}'", 404)
        return item

    def _append(self, **fields: Any) -> LabelRecord:
        base = {
            "source": self.config.source,
            "stage": self.config.stage,
            "labelled_at": utc_now(),
            **fields,
        }
        rec = LabelRecord(record_id=new_record_id(_jsonable(base)), **base)
        self.store.append(rec)
        self.records.append(rec)
        self._refold()
        return rec

    def label(
        self,
        item_id: str,
        labeller: str,
        decision: str | None,
        *,
        skip: bool = False,
        note: str | None = None,
    ) -> LabelRecord:
        labeller = clean_name(labeller)
        note = (note or "").strip() or None
        with self._lock:
            item = self._item(item_id)
            if skip:
                decision = None
            elif decision is None or self.config.decision(decision) is None:
                valid = [d.value for d in self.config.decisions]
                raise LabelError(f"unknown decision {decision!r}; expected one of {valid}")
            if labeller in self.state(item_id).labels:
                raise LabelError(f"{labeller} already labelled '{item_id}'; undo it first", 409)
            rec = self._append(
                item_id=item.id,
                decision=decision,
                skipped=skip,
                labeller=labeller,
                queue=item.queue,
                category=item.category,
                note=note,
            )
            if self._leases.get(labeller, ("",))[0] == item_id:
                self._leases.pop(labeller, None)
            return rec

    def undo(self, labeller: str) -> tuple[LabelRecord, LabelRecord] | None:
        """Retract this labeller's latest active label or skip. Returns (undo, retracted)."""
        labeller = clean_name(labeller)
        with self._lock:
            target = next((r for r in reversed(self.active) if r.labeller == labeller), None)
            if target is None:
                return None
            rec = self._append(
                item_id=target.item_id,
                decision=None,
                skipped=False,
                labeller=labeller,
                queue=target.queue,
                category=target.category,
                undoes=target.record_id,
            )
            self.hold(labeller, target.item_id)
            return rec, target

    # ------------------------------------------------------------------ progress

    def queues(self, labeller: str | None) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            out = []
            for q in self.dataset.queues:
                items = [it for it in self.dataset.items if it.queue == q]
                complete = sum(1 for it in items if self.is_complete(it.id))
                row: dict[str, Any] = {"name": q, "total": len(items), "complete": complete}
                if labeller:
                    mine = sum(1 for it in items if labeller in self.state(it.id).labels)
                    left = sum(
                        1
                        for it in items
                        if labeller not in self.state(it.id).labels
                        and len(self.state(it.id).labels) < self.needed
                    )
                    skipped = sum(
                        1
                        for it in items
                        if labeller in self.state(it.id).skips and self._eligible(it, labeller, now)
                    )
                    row.update({"mine": mine, "left": left, "skipped": skipped})
                out.append(row)
            return out

    def labellers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for st in self.states.values():
            for who in st.labels:
                counts[who] = counts.get(who, 0) + 1
        return dict(sorted(counts.items()))


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LabelError("labeller name is required")
    if len(name) > 80:
        raise LabelError("labeller name is too long (max 80)")
    return name


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.value if hasattr(v, "value") else v) for k, v in d.items()}
