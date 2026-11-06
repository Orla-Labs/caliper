"""Labelling state folded from the append-only log, plus the write operations.

A record is *active* when no later undo record retracts it. For each
(item, labeller) the latest active record wins: a decision is a label, a skip
means "not now". Undo records are never active themselves. Adjudication records
(``source: adjudicated``) are kept apart: they set an item's final label and are
never anyone's "vote", so they do not count towards ``double_label`` or kappa.

An item is *complete* when ``double_label`` distinct labellers have an active
decision on it. A labeller is never served an item they already labelled. Every
card a labeller has on screen or prefetched holds a short lease, so two labellers
working the same queue are not shown the same item when it only needs one more
label.

The :class:`Session` works over two small interfaces so it never needs all
items in memory: an :class:`ItemSource` (paginated reads of the snapshot) and a
:class:`LabelLog` (the append-only records). Legacy ``serve DATA --config CFG``
wraps a JSONL dataset in :class:`DatasetItems`; a project wraps dev 1's
``ProjectStore`` (see ``context.py``). Only labelled items are held in memory.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from caliper_eval.types import LabelRecord, LabelSource

from .project import (
    Assignment,
    GoldenCheck,
    LabelOption,
    LabelsConfig,
    LabelType,
    ScoreRange,
    WorkflowConfig,
)
from .store import LabelStore, new_record_id, utc_now

LEASE_SECONDS = 600.0
MAX_PREFETCH = 20
PAGE = 256
MAX_TEXT = 5000

HUMAN_VOTE_SOURCES = (LabelSource.INTERNAL_REVIEWER, LabelSource.HUMAN_END_USER)


class LabelError(ValueError):
    """A request the state cannot apply (unknown item/decision, already labelled)."""

    def __init__(self, message: str, status: int = 400, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.hint = hint


# --------------------------------------------------------------------------- interfaces


@dataclass
class ItemView:
    """One snapshot item as the labelling loop sees it."""

    id: str
    ordinal: int
    queue: str
    category: str | None = None
    golden_answer: str | None = None  # never sent to the browser
    data: Any = None  # whatever the ItemSource needs to render the card


class ItemSource(Protocol):
    """Paginated, read-only view of the items (the project snapshot)."""

    def queues(self) -> list[str]: ...

    def count(self, queue: str | None = None) -> int: ...

    def page(self, offset: int, limit: int, queue: str | None = None) -> list[ItemView]: ...

    def get(self, item_id: str) -> ItemView | None: ...

    def golden_ids(self) -> list[str]: ...  # items with a known answer (small set)

    def card(self, item: ItemView) -> list[dict[str, Any]]: ...  # display fields, JSON-safe


class LabelLog(Protocol):
    """Append-only label records, each stamped with the project version it was made under."""

    def load(self) -> list[tuple[LabelRecord, int | None]]: ...

    def append(self, record: LabelRecord, project_version: int | None) -> None: ...


@dataclass
class ItemState:
    labels: dict[str, LabelRecord]  # labeller -> active decision record
    skips: dict[str, LabelRecord]  # labeller -> active skip record (no later label)
    adjudication: LabelRecord | None = None
    queue: str | None = None


def is_adjudication(r: LabelRecord) -> bool:
    return r.source is LabelSource.ADJUDICATED


def fold(records: Iterable[LabelRecord]) -> tuple[dict[str, ItemState], list[LabelRecord]]:
    """Return per-item state and the active label/skip/adjudication records in log order."""
    records = list(records)
    undone = {r.undoes for r in records if r.undoes}
    active = [r for r in records if not r.undoes and r.record_id not in undone]
    states: dict[str, ItemState] = {}
    for r in active:
        st = states.setdefault(r.item_id, ItemState({}, {}, None, r.queue))
        if is_adjudication(r):
            st.adjudication = r
        elif r.skipped or r.decision is None:
            st.skips[r.labeller] = r
            st.labels.pop(r.labeller, None)
        else:
            st.labels[r.labeller] = r
            st.skips.pop(r.labeller, None)
    return states, active


# --------------------------------------------------------------------------- label rules


DEFAULT_YNU = [
    LabelOption(value="yes", label="Yes", hotkey="1", approved=True, color="#2e7d32"),
    LabelOption(value="no", label="No", hotkey="2", color="#c62828"),
    LabelOption(value="unsure", label="Unsure", hotkey="3", color="#8a6d00"),
]


def effective_options(labels: LabelsConfig) -> list[LabelOption]:
    """Options with hotkeys filled in (1..9 for those without one)."""
    opts = list(labels.options)
    if labels.type is LabelType.YES_NO_UNSURE and not opts:
        opts = [o.model_copy() for o in DEFAULT_YNU]
    taken = {o.hotkey.lower() for o in opts if o.hotkey}
    reserved = {labels.keys.skip, labels.keys.note, labels.keys.undo, "z", "?"}
    free = [k for k in "123456789" if k not in taken and k not in reserved]
    out = []
    for o in opts:
        if not o.hotkey and not o.retired and free:
            o = o.model_copy(update={"hotkey": free.pop(0)})
        out.append(o)
    return out


def score_values(rng: ScoreRange) -> list[float]:
    if rng.step <= 0:
        return []
    n = round((rng.max - rng.min) / rng.step)
    if n < 0 or n > 100:
        return []
    return [round(rng.min + i * rng.step, 6) for i in range(n + 1)]


def _num_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


@dataclass
class LabelRules:
    """How decisions are validated and encoded for one project's label type.

    ``LabelRecord.decision`` is a string, so every type has one canonical encoding:

    * single / yes_no_unsure: the option ``value``;
    * multi: a JSON array of option values, sorted (``["a","b"]``);
    * score: the number as text (``"4"``, ``"2.5"``);
    * free_text: the text itself (trimmed, at most 5000 characters).
    """

    labels: LabelsConfig
    options: list[LabelOption] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.options = effective_options(self.labels)

    @property
    def type(self) -> LabelType:
        return self.labels.type

    @property
    def score(self) -> ScoreRange:
        return self.labels.score or ScoreRange()

    def option(self, value: str) -> LabelOption | None:
        for o in self.options:
            if o.value == value:
                return o
        return None

    def chosen(self, decision: str | None) -> list[LabelOption]:
        """The options a canonical decision names (empty for score / free text)."""
        if decision is None:
            return []
        if self.type is LabelType.MULTI:
            try:
                vals = json.loads(decision)
            except ValueError:
                vals = [decision]
            if not isinstance(vals, list):
                vals = [vals]
            return [o for v in vals if (o := self.option(str(v)))]
        if self.type in (LabelType.SINGLE, LabelType.YES_NO_UNSURE):
            o = self.option(decision)
            return [o] if o else []
        return []

    def canonical(self, decision: Any, note: str | None) -> str:
        """Validate a client decision and return its stored form, or raise LabelError."""
        t = self.type
        if t in (LabelType.SINGLE, LabelType.YES_NO_UNSURE):
            if isinstance(decision, list) and len(decision) == 1:
                decision = decision[0]
            if not isinstance(decision, str):
                raise LabelError(f"expected one option value, got {decision!r}")
            opts = [self._active_option(decision)]
            value = decision
        elif t is LabelType.MULTI:
            vals = decision if isinstance(decision, list) else [decision]
            if isinstance(decision, str) and decision.startswith("["):
                try:
                    vals = json.loads(decision)
                except ValueError:
                    pass
            vals = sorted({str(v) for v in vals if v is not None and str(v) != ""})
            if not vals:
                raise LabelError("pick at least one option", hint="or skip the item")
            opts = [self._active_option(v) for v in vals]
            value = json.dumps(vals, ensure_ascii=False)
        elif t is LabelType.SCORE:
            try:
                num = float(decision)
            except (TypeError, ValueError):
                raise LabelError(f"expected a number, got {decision!r}") from None
            rng = self.score
            if not math.isfinite(num) or num < rng.min or num > rng.max:
                raise LabelError(
                    f"score must be between {_num_text(rng.min)} and {_num_text(rng.max)}"
                )
            if rng.step > 0:
                k = (num - rng.min) / rng.step
                if abs(k - round(k)) > 1e-6:
                    raise LabelError(f"score must be in steps of {_num_text(rng.step)}")
            return _num_text(num)
        else:  # free text
            text = (decision if isinstance(decision, str) else "").strip()
            if not text:
                raise LabelError("the answer is empty", hint="type an answer, or skip the item")
            if len(text) > MAX_TEXT:
                raise LabelError(f"the answer is too long (max {MAX_TEXT} characters)")
            return text
        needs = [o for o in opts if o.requires_note]
        if needs and not (note or "").strip():
            names = ", ".join(o.label or o.value for o in needs)
            raise LabelError(
                f"'{names}' needs a note",
                422,
                hint="press N (or click Note), write why, then decide",
            )
        return value

    def _active_option(self, value: str) -> LabelOption:
        o = self.option(value)
        if o is None:
            valid = [x.value for x in self.options if not x.retired]
            raise LabelError(f"unknown decision {value!r}; expected one of {valid}")
        if o.retired:
            raise LabelError(
                f"option {value!r} was retired in project settings",
                hint="reload the page to get the current options",
            )
        return o

    def human_label(self, decision: str) -> float | None:
        """0..1 score for rollouts exports (None: this type has no numeric meaning)."""
        if self.type is LabelType.SCORE:
            rng = self.score
            span = rng.max - rng.min
            return (float(decision) - rng.min) / span if span else 1.0
        chosen = self.chosen(decision)
        if not chosen:
            return None

        def one(o: LabelOption) -> float:
            if o.score is not None:
                return o.score
            return 1.0 if o.approved else 0.0

        return max(one(o) for o in chosen)

    def approved(self, decision: str) -> bool | None:
        if self.type is LabelType.SCORE:
            hl = self.human_label(decision)
            return None if hl is None else hl >= 0.5
        chosen = self.chosen(decision)
        if not chosen:
            return None
        return all(o.approved for o in chosen)

    def outcome(self, decision: str) -> str:
        chosen = self.chosen(decision)
        if len(chosen) == 1:
            return chosen[0].outcome or chosen[0].value
        return decision

    def display(self, decision: str | None) -> str | None:
        if decision is None:
            return None
        chosen = self.chosen(decision)
        if chosen:
            return ", ".join(o.label or o.value for o in chosen)
        return decision

    def matches(self, decision: str, answer: str) -> bool:
        """Does a stored decision match a golden item's known answer?"""
        a = answer.strip()
        if self.type is LabelType.MULTI:
            try:
                want = json.loads(a) if a.startswith("[") else [x.strip() for x in a.split(",")]
            except ValueError:
                want = [a]
            got = {o.value for o in self.chosen(decision)}
            return got == {self._resolve(str(w)) for w in want if str(w).strip()}
        if self.type is LabelType.SCORE:
            try:
                return abs(float(decision) - float(a)) < 1e-9
            except ValueError:
                return False
        if self.type is LabelType.FREE_TEXT:
            return decision.strip().casefold() == a.casefold()
        return decision == self._resolve(a)

    def _resolve(self, answer: str) -> str:
        """A golden answer may name an option by value, label or outcome (case-insensitive)."""
        low = answer.casefold()
        for o in self.options:
            if low in {
                o.value.casefold(),
                (o.label or "").casefold(),
                (o.outcome or "").casefold(),
            }:
                return o.value
        return answer


# --------------------------------------------------------------------------- session


class Session:
    """Thread-safe labelling session over one item source and one label log.

    ``Session(dataset, config, store)`` keeps the legacy (``serve DATA --config``)
    signature. Projects use :meth:`Session.over`.
    """

    def __init__(self, dataset: Any, config: Any, store: LabelStore) -> None:
        from .legacy_session import DatasetItems, StoreLog, legacy_labels, legacy_workflow

        self.dataset = dataset
        self.config = config
        self.store = store
        self._setup(
            DatasetItems(dataset, config),
            StoreLog(store),
            legacy_labels(config),
            legacy_workflow(config),
            project_version=None,
        )
        unknown = {r.item_id for r in self.records} - set(dataset.by_id)
        self.unknown_items = sorted(unknown)  # labels for rows not in this data file

    @classmethod
    def over(
        cls,
        items: ItemSource,
        log: LabelLog,
        labels: LabelsConfig,
        workflow: WorkflowConfig,
        *,
        project_version: int | None = None,
    ) -> Session:
        self = cls.__new__(cls)
        self.dataset = None
        self.config = None
        self.store = None
        self._setup(items, log, labels, workflow, project_version=project_version)
        self.unknown_items = []
        return self

    def _setup(
        self,
        items: ItemSource,
        log: LabelLog,
        labels: LabelsConfig,
        workflow: WorkflowConfig,
        *,
        project_version: int | None,
    ) -> None:
        self.items = items
        self.log = log
        self.labels_cfg = labels
        self.workflow = workflow
        self.rules = LabelRules(labels)
        self.project_version = project_version
        self._lock = threading.RLock()
        loaded = log.load()
        self.records: list[LabelRecord] = [r for r, _ in loaded]
        self.versions: dict[str, int | None] = {r.record_id: v for r, v in loaded}
        self._golden = set(items.golden_ids()) if workflow.golden else set()
        self._leases: dict[str, dict[str, float]] = {}  # labeller -> {item_id: expires}
        self._cursors: dict[tuple[str, str | None], int] = {}
        self._roster: list[str] = []  # round-robin order: first seen
        self._refold()
        for r in self.records:
            self._join(r.labeller, r)

    def reconfigure(
        self, labels: LabelsConfig, workflow: WorkflowConfig, project_version: int | None
    ) -> None:
        """Settings were saved: new options / workflow, same log."""
        with self._lock:
            self.labels_cfg, self.workflow = labels, workflow
            self.rules = LabelRules(labels)
            self.project_version = project_version
            self._golden = set(self.items.golden_ids()) if workflow.golden else set()
            self._cursors.clear()

    # ------------------------------------------------------------------ folding

    def _refold(self) -> None:
        self.states, self.active = fold(self.records)

    def state(self, item_id: str) -> ItemState:
        return self.states.get(item_id) or ItemState({}, {})

    @property
    def needed(self) -> int:
        return self.workflow.double_label

    def is_complete(self, item_id: str) -> bool:
        return len(self.state(item_id).labels) >= self.needed

    def is_golden(self, item_id: str) -> bool:
        return item_id in self._golden

    def _join(self, labeller: str, rec: LabelRecord | None = None) -> None:
        if rec is not None and is_adjudication(rec):
            return
        if labeller not in self._roster:
            self._roster.append(labeller)
            self._cursors.clear()  # round-robin shares changed

    # ------------------------------------------------------------------ serving

    def _leased_by_others(self, item_id: str, labeller: str, now: float) -> int:
        return sum(
            1
            for who, held in self._leases.items()
            if who != labeller and held.get(item_id, 0) > now
        )

    def _assigned(self, item: ItemView, labeller: str) -> bool:
        if self.workflow.assignment is not Assignment.ROUND_ROBIN:
            return True
        n = len(self._roster)
        if n <= self.needed or labeller not in self._roster:
            return True
        slot = self._roster.index(labeller)
        return any((item.ordinal + j) % n == slot for j in range(self.needed))

    def _eligible(self, item: ItemView, labeller: str, now: float) -> bool:
        st = self.state(item.id)
        if labeller in st.labels:
            return False
        if st.adjudication is not None:
            return False
        others = len(st.labels) + self._leased_by_others(item.id, labeller, now)
        return others < self.needed

    def next_items(self, labeller: str, queue: str | None = None, n: int = 1) -> list[ItemView]:
        """The next ``n`` items this labeller may label, in serving order.

        Order: items someone else already labelled (with double labelling, so the
        overlap set fills early), then fresh items in data order, then items this
        labeller skipped. Golden check items are mixed in at the configured rate.
        All returned items are leased to the labeller (their previous leases go).
        """
        n = max(1, min(int(n), MAX_PREFETCH))
        with self._lock:
            self._join(labeller)
            now = time.monotonic()
            plain = self._plain_items(labeller, queue, n, now)
            out = self._mix_golden(labeller, queue, plain, n)
            self._leases[labeller] = {it.id: now + LEASE_SECONDS for it in out}
            return out

    def next_item(self, labeller: str, queue: str | None = None) -> ItemView | None:
        got = self.next_items(labeller, queue, 1)
        return got[0] if got else None

    def _plain_items(self, labeller: str, queue: str | None, n: int, now: float) -> list[ItemView]:
        started: list[ItemView] = []
        if self.needed > 1:
            partial = sorted(
                (
                    iid
                    for iid, st in self.states.items()
                    if 0 < len(st.labels) < self.needed
                    and labeller not in st.labels
                    and labeller not in st.skips
                    and iid not in self._golden
                    and (queue is None or st.queue == queue)
                ),
            )
            for iid in partial:
                it = self.items.get(iid)
                if it is None or (queue and it.queue != queue):
                    continue
                if self._eligible(it, labeller, now) and self._assigned(it, labeller):
                    started.append(it)
            started.sort(key=lambda it: it.ordinal)
            started = started[:n]
        fresh: list[ItemView] = []
        skipped: list[ItemView] = []
        seen = {it.id for it in started}
        key = (labeller, queue)
        offset = self._cursors.get(key, 0)
        advancing = True
        total = self.items.count(queue)
        while offset < total and len(started) + len(fresh) < n:
            page = self.items.page(offset, PAGE, queue)
            if not page:
                break
            for i, it in enumerate(page):
                st = self.state(it.id)
                gone = (
                    it.id in self._golden
                    or labeller in st.labels
                    or st.adjudication is not None
                    or len(st.labels) >= self.needed
                    or not self._assigned(it, labeller)
                )
                if gone:
                    if advancing:
                        self._cursors[key] = offset + i + 1
                    continue
                advancing = False
                if it.id in seen or not self._eligible(it, labeller, now):
                    continue
                if labeller in st.skips:
                    skipped.append(it)
                    continue
                fresh.append(it)
                if len(started) + len(fresh) >= n:
                    break
            offset += len(page)
        out = started + fresh
        if len(out) < n:
            for iid, st in sorted(self.states.items()):
                if len(skipped) >= n:
                    break
                if labeller in st.skips and iid not in {s.id for s in skipped}:
                    it = self.items.get(iid)
                    if (
                        it
                        and (queue is None or it.queue == queue)
                        and iid not in self._golden
                        and self._eligible(it, labeller, now)
                        and self._assigned(it, labeller)
                    ):
                        skipped.append(it)
            skipped.sort(key=lambda it: it.ordinal)
            out += skipped[: n - len(out)]
        return out

    def _mix_golden(
        self, labeller: str, queue: str | None, plain: list[ItemView], n: int
    ) -> list[ItemView]:
        """Insert hidden golden items at ``workflow.golden.rate``.

        Whether position ``p`` of a labeller's stream is a golden check is drawn from
        an RNG seeded by (labeller, p), so it is stable across prefetch calls but
        cannot be spotted by position. Each labeller sees each golden item once.
        """
        g: GoldenCheck | None = self.workflow.golden
        if not g or not self._golden or g.rate <= 0 or not plain:
            return plain
        mine = [
            iid
            for iid in self._golden
            if labeller not in self.state(iid).labels and labeller not in self.state(iid).skips
        ]
        if not mine:
            return plain
        mine.sort()
        random.Random(f"order:{labeller}").shuffle(mine)  # noqa: S311 (seeded, not crypto)
        base = sum(1 for r in self.active if r.labeller == labeller and not is_adjudication(r))
        out: list[ItemView] = []
        plain_iter = iter(plain)
        gold_iter = iter(mine)
        p = base
        while len(out) < n:
            if random.Random(f"slot:{labeller}:{p}").random() < g.rate:  # noqa: S311
                gid = next(gold_iter, None)
                it = self.items.get(gid) if gid else None
                if it is not None and (queue is None or it.queue == queue):
                    out.append(it)
                    p += 1
                    continue
            nxt = next(plain_iter, None)
            if nxt is None:
                break
            out.append(nxt)
            p += 1
        return out

    def hold(self, labeller: str, item_id: str) -> None:
        with self._lock:
            self._leases.setdefault(labeller, {})[item_id] = time.monotonic() + LEASE_SECONDS

    # ------------------------------------------------------------------ writing

    def _item(self, item_id: str) -> ItemView:
        item = self.items.get(item_id)
        if item is None:
            raise LabelError(f"unknown item '{item_id}'", 404)
        return item

    def _append(self, *, source: LabelSource | None = None, **fields: Any) -> LabelRecord:
        base = {
            "source": source or self.workflow.source,
            "stage": self.workflow.stage,
            "labelled_at": utc_now(),
            **fields,
        }
        rec = LabelRecord(record_id=new_record_id(_jsonable(base)), **base)
        self.log.append(rec, self.project_version)
        self.records.append(rec)
        self.versions[rec.record_id] = self.project_version
        self._refold()
        return rec

    def label(
        self,
        item_id: str,
        labeller: str,
        decision: Any,
        *,
        skip: bool = False,
        note: str | None = None,
    ) -> LabelRecord:
        labeller = clean_name(labeller)
        note = (note or "").strip() or None
        if note and len(note) > 2000:
            raise LabelError("note is too long (max 2000 characters)")
        with self._lock:
            item = self._item(item_id)
            if skip:
                value = None
            elif decision is None:
                raise LabelError("decision is required (or send skip: true)")
            else:
                value = self.rules.canonical(decision, note)
            if labeller in self.state(item_id).labels:
                raise LabelError(f"{labeller} already labelled '{item_id}'; undo it first", 409)
            self._join(labeller)
            rec = self._append(
                item_id=item.id,
                decision=value,
                skipped=skip,
                labeller=labeller,
                queue=item.queue,
                category=item.category,
                note=note,
            )
            self._leases.get(labeller, {}).pop(item_id, None)
            return rec

    def undo(self, labeller: str) -> tuple[LabelRecord, LabelRecord] | None:
        """Retract this labeller's latest active label or skip. Returns (undo, retracted)."""
        labeller = clean_name(labeller)
        with self._lock:
            target = next(
                (
                    r
                    for r in reversed(self.active)
                    if r.labeller == labeller and not is_adjudication(r)
                ),
                None,
            )
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
                source=target.source,
            )
            self._cursors.clear()
            self.hold(labeller, target.item_id)
            return rec, target

    def adjudicate(
        self, item_id: str, adjudicator: str, decision: Any, note: str | None = None
    ) -> LabelRecord:
        """Set an item's final label (``source: adjudicated``). A later one replaces it."""
        adjudicator = clean_name(adjudicator)
        note = (note or "").strip() or None
        with self._lock:
            item = self._item(item_id)
            if decision is None:
                raise LabelError("decision is required")
            value = self.rules.canonical(decision, note)
            prev = self.state(item_id).adjudication
            if prev is not None:  # replace: retract the previous adjudication first
                self._append(
                    item_id=item.id,
                    decision=None,
                    labeller=adjudicator,
                    queue=item.queue,
                    category=item.category,
                    undoes=prev.record_id,
                    source=LabelSource.ADJUDICATED,
                )
            return self._append(
                item_id=item.id,
                decision=value,
                skipped=False,
                labeller=adjudicator,
                queue=item.queue,
                category=item.category,
                note=note,
                source=LabelSource.ADJUDICATED,
            )

    # ------------------------------------------------------------------ progress

    def queues(self, labeller: str | None) -> list[dict[str, Any]]:
        """Per-queue progress. Counts come from the labelled items only (no full scan)."""
        with self._lock:
            now = time.monotonic()
            out = []
            gold_by_q: dict[str | None, int] = {}
            for gid in self._golden:
                g = self.items.get(gid)
                if g is not None:
                    gold_by_q[g.queue] = gold_by_q.get(g.queue, 0) + 1
            for q in self.items.queues():
                total = self.items.count(q) - gold_by_q.get(q, 0)
                complete = mine = gone_for_me = skipped = 0
                for iid, st in self.states.items():
                    if st.queue != q or iid in self._golden:
                        continue
                    done = len(st.labels) >= self.needed or st.adjudication is not None
                    complete += done
                    if labeller:
                        is_mine = labeller in st.labels
                        mine += is_mine
                        gone_for_me += done or is_mine
                        if labeller in st.skips and not done:
                            it = self.items.get(iid)
                            skipped += bool(it and self._eligible(it, labeller, now))
                row: dict[str, Any] = {"name": q, "total": total, "complete": complete}
                if labeller:
                    row.update({"mine": mine, "left": total - gone_for_me, "skipped": skipped})
                out.append(row)
            return out

    def labellers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for iid, st in self.states.items():
            if iid in self._golden:
                continue
            for who in st.labels:
                counts[who] = counts.get(who, 0) + 1
        return dict(sorted(counts.items()))

    def vote_records(self) -> list[LabelRecord]:
        """Active human votes on non-golden items (what kappa and consensus use)."""
        return [
            r
            for r in self.active
            if not is_adjudication(r)
            and not r.skipped
            and r.decision is not None
            and r.item_id not in self._golden
        ]

    def golden_accuracy(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not self._golden:
            return out
        for gid in self._golden:
            it = self.items.get(gid)
            if it is None or not it.golden_answer:
                continue
            for who, r in self.state(gid).labels.items():
                row = out.setdefault(who, {"answered": 0, "correct": 0})
                row["answered"] += 1
                row["correct"] += self.rules.matches(str(r.decision), it.golden_answer)
        for row in out.values():
            row["accuracy"] = row["correct"] / row["answered"] if row["answered"] else None
        return dict(sorted(out.items()))


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LabelError("labeller name is required", hint="enter your name before labelling")
    if len(name) > 80:
        raise LabelError("labeller name is too long (max 80)")
    return name


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.value if hasattr(v, "value") else v) for k, v in d.items()}
