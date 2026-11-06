"""Export labels: golden rows, labeled rollouts, the raw record log, or a label table.

Consensus per item, from the active votes (skips never count):

* an adjudicated final label (``source: adjudicated``) -> resolved to it, always;
* fewer than ``double_label`` votes   -> *incomplete*, left out;
* all votes agree                     -> resolved to that decision;
* votes disagree                      -> *conflict*. Policy ``exclude`` (default)
  leaves it out; ``majority`` takes a strict majority and leaves ties out.

Everything left out is listed in the summary so nothing disappears silently.

Formats:

* ``golden``   GoldenRow JSONL with ``label`` / ``label_approved`` (``caliper eval``);
* ``rollouts`` EvalRow JSONL with ``rollout.metadata.human_label`` (``caliper kappa``);
* ``records``  the raw ``LabelRecord`` log (every label, skip, undo, adjudication);
* ``jsonl`` / ``csv`` / ``parquet`` a label table: one row per item with its final
  label (``rows="items"``, default) or one row per label (``rows="labels"``). Every
  row carries labeller, timestamp, note, source, stage and project version.
  ``approved`` / ``human_label`` are only there when they mean something: the project
  has an approved option, a scored option, or a score label type. In the table an
  abstaining option (Unsure / Uncertain / Unknown ...) without a ``score`` gets
  ``human_label: null``, not 0. Golden and rollouts keep their own encoding.
  CSV / parquet / table rows are flat (``item.<column>``); JSON lines nest the item
  (``"item": {...}``) with the card's JSON columns parsed.
* ``table``    the label table written back to a database table (dev 1's sinks).
"""

from __future__ import annotations

import copy
import csv
import io
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from caliper_eval.types import GoldenRow, LabelRecord

from .config import LabelConfig, get_path
from .data import DataKind, Dataset
from .state import fold

if TYPE_CHECKING:
    from .state import ItemView, Session


class ExportFormat(StrEnum):
    GOLDEN = "golden"
    ROLLOUTS = "rollouts"
    RECORDS = "records"
    JSONL = "jsonl"
    CSV = "csv"
    PARQUET = "parquet"
    TABLE = "table"


TABULAR = {ExportFormat.JSONL, ExportFormat.CSV, ExportFormat.PARQUET, ExportFormat.TABLE}
FORMAT_HELP = {
    "golden": "GoldenRow JSONL with label / label_approved, for caliper eval",
    "rollouts": "EvalRow JSONL with rollout.metadata.human_label, for caliper kappa",
    "records": "the raw LabelRecord log: every label, skip, undo and adjudication",
    "jsonl": "label table as JSON lines",
    "csv": "label table as CSV (opens in a spreadsheet)",
    "parquet": "label table as Parquet",
    "table": "label table written to a database table",
}
# Plain words first for people (usability #11); FORMAT_HELP keeps the technical detail.
FORMAT_TITLE = {
    "csv": ("Spreadsheet (CSV)", "one row per item; opens in Excel, Sheets or Numbers"),
    "jsonl": ("JSON lines", "one JSON object per item, for scripts and pipelines"),
    "parquet": ("Parquet", "a compact table for data tools (pandas, DuckDB, Spark)"),
    "golden": (
        "Answer key for evaluations",
        "your labels as reference answers for an eval run",
    ),
    "rollouts": (
        "Human scores to compare with a judge",
        "each item with a 0-1 human score, to measure how well a judge agrees",
    ),
    "records": ("Full label history", "every label, skip, undo and adjudication, in order"),
    "table": ("Database table", "the label table, written into a database"),
}

# Options that mean "I can't tell": no numeric meaning in the label table unless scored.
ABSTAIN = {
    "unsure",
    "uncertain",
    "unknown",
    "not_sure",
    "not sure",
    "maybe",
    "cant_tell",
    "can't tell",
    "cannot tell",
    "dont_know",
    "don't know",
    "idk",
}


class ConflictPolicy(StrEnum):
    EXCLUDE = "exclude"
    MAJORITY = "majority"


class RowsMode(StrEnum):
    ITEMS = "items"
    LABELS = "labels"


class ExportError(ValueError):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class Resolved:
    decision: str
    records: list[LabelRecord]
    status: str = "agreed"  # agreed | majority | adjudicated
    votes: list[LabelRecord] = field(default_factory=list)


@dataclass
class Consensus:
    resolved: dict[str, Resolved] = field(default_factory=dict)
    conflicts: dict[str, dict[str, str]] = field(default_factory=dict)  # item -> labeller->dec
    incomplete: dict[str, int] = field(default_factory=dict)  # item -> labels so far
    unlabelled: int = 0


def resolve_item(
    labels: list[LabelRecord],
    adjudication: LabelRecord | None,
    needed: int,
    policy: ConflictPolicy,
) -> Resolved | tuple[str, Any] | None:
    """Resolved, ("conflict", {who: dec}), ("incomplete", n), or None (no labels)."""
    if adjudication is not None:
        return Resolved(str(adjudication.decision), [adjudication], "adjudicated", labels)
    if not labels:
        return None
    if len(labels) < needed:
        return ("incomplete", len(labels))
    counts = Counter(r.decision for r in labels)
    top, top_n = counts.most_common(1)[0]
    if len(counts) == 1:
        return Resolved(str(top), labels, "agreed" if len(labels) > 1 else "single", labels)
    if policy is ConflictPolicy.MAJORITY and top_n * 2 > len(labels):
        return Resolved(str(top), [r for r in labels if r.decision == top], "majority", labels)
    return ("conflict", {r.labeller: str(r.decision) for r in labels})


def _fill(out: Consensus, iid: str, res: Resolved | tuple[str, Any] | None) -> None:
    if res is None:
        out.unlabelled += 1
    elif isinstance(res, Resolved):
        out.resolved[iid] = res
    elif res[0] == "incomplete":
        out.incomplete[iid] = res[1]
    else:
        out.conflicts[iid] = res[1]


def consensus(
    dataset: Dataset,
    config: LabelConfig,
    records: list[LabelRecord],
    policy: ConflictPolicy = ConflictPolicy.EXCLUDE,
) -> Consensus:
    """Legacy (``serve DATA --config``) consensus over every row of the data file."""
    states, _ = fold(records)
    out = Consensus()
    for item in dataset.items:
        st = states.get(item.id)
        labels = list(st.labels.values()) if st else []
        _fill(
            out,
            item.id,
            resolve_item(labels, st.adjudication if st else None, config.double_label, policy),
        )
    return out


def session_consensus(session: Session, policy: ConflictPolicy) -> Consensus:
    """Consensus from the labelled items only (no scan of the snapshot)."""
    out = Consensus()
    labelled = 0
    for iid, st in session.states.items():
        if session.is_golden(iid) or session.items.get(iid) is None:
            continue
        res = resolve_item(list(st.labels.values()), st.adjudication, session.needed, policy)
        if res is None:
            continue
        labelled += 1
        _fill(out, iid, res)
    total = session.items.count(None) - len(session._golden)
    out.unlabelled = max(0, total - labelled)
    return out


def summary(
    cons: Consensus, session: Session | None = None, policy: str = "exclude"
) -> dict[str, Any]:
    rules = session.rules if session else None
    show = rules.display if rules else (lambda d: d)
    return {
        "policy": policy,
        "resolved": len(cons.resolved),
        "adjudicated": sum(1 for r in cons.resolved.values() if r.status == "adjudicated"),
        "conflicts": len(cons.conflicts),
        "incomplete": len(cons.incomplete),
        "unlabelled": cons.unlabelled,
        "left_out": [
            {
                "item_id": iid,
                "why": "conflict",
                "labels": {k: show(v) for k, v in sorted(lab.items())},
            }
            for iid, lab in sorted(cons.conflicts.items())
        ]
        + [
            {"item_id": iid, "why": "incomplete", "labels_so_far": n}
            for iid, n in sorted(cons.incomplete.items())
        ],
    }


# --------------------------------------------------------------------------- legacy rows


def _human_label(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _provenance(res: Resolved, version: int | None = None) -> dict[str, Any]:
    recs = sorted(res.records, key=lambda r: r.labelled_at)
    # who decided: the agreeing voters (all voters when an adjudicator settled it)
    voters = res.votes if res.status == "adjudicated" else res.records
    votes = sorted(voters, key=lambda r: r.labelled_at)
    out = {
        "human_decision": res.decision,
        "label_source": recs[0].source.value,
        "label_stage": recs[0].stage,
        "labellers": [r.labeller for r in votes if r.source.value != "adjudicated"],
        "labelled_at": recs[-1].labelled_at,
        "notes": [r.note for r in recs if r.note] or None,
    }
    if res.status == "adjudicated":
        out["adjudicator"] = recs[0].labeller
        out["resolution"] = "adjudicated"
    elif res.status == "majority":
        out["resolution"] = "majority"
    if version is not None:
        out["project_version"] = version
    return out


def golden_rows(
    dataset: Dataset, config: LabelConfig, cons: Consensus, include_unlabelled: bool = False
) -> list[dict[str, Any]]:
    if dataset.kind is not DataKind.GOLDEN:
        raise ExportError(
            f"--format golden needs GoldenRow input; {dataset.path} is {dataset.kind.value} "
            "rows (use --format rollouts or records)",
            "use --format rollouts, records, csv or jsonl",
        )
    out = []
    for item in dataset.items:
        res = cons.resolved.get(item.id)
        if res is None:
            if include_unlabelled:
                out.append(item.raw)
            continue
        spec = config.decision(res.decision)
        assert spec is not None
        recs = sorted(res.records, key=lambda r: r.labelled_at)
        row = copy.deepcopy(item.raw)
        row.update(
            label=spec.outcome or spec.value,
            label_approved=spec.approved,
            label_source=recs[0].source.value,
            label_stage=recs[0].stage,
        )
        row.setdefault("metadata", {})
        row["metadata"]["labelling"] = {
            k: v for k, v in _provenance(res).items() if k not in ("label_source", "label_stage")
        }
        GoldenRow.model_validate(row)  # still a valid golden row
        out.append(row)  # written as-is: only the label fields above change
    return out


def _render_request(request: dict[str, Any]) -> str:
    parts = []
    if request.get("system"):
        parts.append(f"[system]\n{request['system']}")
    for m in request.get("messages") or []:
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"[{m.get('role', 'user')}]\n{content}")
    return "\n\n".join(parts)


def _text(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)


def rollout_rows(dataset: Dataset, config: LabelConfig, cons: Consensus) -> list[dict[str, Any]]:
    """EvalRow JSON with ``rollout.metadata.human_label`` (0..1), as caliper_kappa expects."""
    from caliper_sdk import EvalRow

    out = []
    rs = config.rollout
    for item in dataset.items:
        res = cons.resolved.get(item.id)
        if res is None:
            continue
        spec = config.decision(res.decision)
        assert spec is not None
        raw = item.raw
        if dataset.kind is DataKind.EVALROW:
            row = copy.deepcopy(raw)
            meta = row["rollout"].setdefault("metadata", {})
        else:
            prompt = _text(get_path(raw, rs.prompt)) if rs.prompt else None
            if prompt is None:
                req = raw.get("request")
                prompt = _render_request(req) if isinstance(req, dict) else _text(raw) or ""
            response = _text(get_path(raw, rs.response)) if rs.response else None
            if response is None:
                response = _text(raw.get("reference_outcome")) or ""
            gold = _text(get_path(raw, rs.gold)) if rs.gold else None
            meta = (
                copy.deepcopy(raw.get("metadata") or {})
                if isinstance(raw.get("metadata"), dict)
                else {}
            )
            if raw.get("stratum") is not None:
                meta.setdefault("stratum", raw["stratum"])
            row = {
                "id": item.id,
                "rollout": {"prompt": prompt, "response": response, "gold": gold, "metadata": meta},
            }
        meta["human_label"] = _human_label(spec.human_label)
        meta.update({k: v for k, v in _provenance(res).items() if v is not None})
        out.append(EvalRow.model_validate(row).model_dump(mode="json"))
    return out


def export(
    dataset: Dataset,
    config: LabelConfig,
    records: list[LabelRecord],
    out_path: str | Path,
    fmt: ExportFormat,
    policy: ConflictPolicy = ConflictPolicy.EXCLUDE,
    include_unlabelled: bool = False,
) -> tuple[int, Consensus]:
    """Legacy export to a JSONL file; return (rows written, consensus) for the summary."""
    cons = consensus(dataset, config, records, policy)
    if fmt is ExportFormat.RECORDS:
        rows = [r.model_dump(mode="json") for r in records]
    elif fmt is ExportFormat.GOLDEN:
        rows = golden_rows(dataset, config, cons, include_unlabelled)
    elif fmt is ExportFormat.ROLLOUTS:
        rows = rollout_rows(dataset, config, cons)
    else:
        raise ExportError(f"format {fmt.value} needs a Session; use export_session()")
    n = write_jsonl(rows, out_path)
    return n, cons


# --------------------------------------------------------------------------- session rows


def _row_of(session: Session, item: ItemView) -> dict[str, Any]:
    """The item's data as a flat-ish dict: the raw row (legacy) or the snapshot display."""
    data = item.data
    if hasattr(data, "raw"):  # legacy Item
        return data.raw
    return dict(data or {})


def _items_in_order(session: Session, ids: Iterable[str]) -> list[ItemView]:
    got = [session.items.get(i) for i in ids]
    return sorted((g for g in got if g is not None), key=lambda it: it.ordinal)


def _rec_row(session: Session, r: LabelRecord) -> dict[str, Any]:
    return {
        "labeller": r.labeller,
        "labelled_at": r.labelled_at,
        "note": r.note,
        "source": r.source.value,
        "stage": r.stage,
        "project_version": session.versions.get(r.record_id),
    }


def _flat(v: Any) -> Any:
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def scored(rules: Any) -> bool:
    """Do approved / human_label mean anything for this project's labels?"""
    from .project import LabelType

    if rules.type is LabelType.SCORE:
        return True
    return any(o.approved or o.score is not None for o in rules.options)


def is_abstain(option: Any) -> bool:
    names = {str(option.value).strip().lower(), str(option.label or "").strip().lower()}
    return option.score is None and not option.approved and bool(names & ABSTAIN)


def table_scores(rules: Any, decision: str) -> tuple[bool | None, float | int | None]:
    """(approved, human_label) for the label table: an abstaining option scores null."""
    chosen = rules.chosen(decision)
    if chosen and all(is_abstain(o) for o in chosen):
        return None, None
    hl = rules.human_label(decision)
    return rules.approved(decision), None if hl is None else _human_label(hl)


def json_columns(session: Session) -> set[str]:
    """Columns the card shows as JSON: parsed into objects in JSON lines exports."""
    cfg = getattr(session.items, "config", None)
    card = getattr(cfg, "card", None)
    if card is None:
        return set()
    from .project import DisplayType

    return {f.column for f in card.fields if f.type is DisplayType.JSON and f.column}


def nest_item(row: dict[str, Any], parse: set[str] = frozenset()) -> dict[str, Any]:
    """``item.<col>`` keys -> ``item: {col: ...}`` (one level: column names may hold dots)."""
    out: dict[str, Any] = {}
    item: dict[str, Any] = {}
    for k, v in row.items():
        if k.startswith("item."):
            col = k[5:]
            if col in parse and isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            item[col] = v
        else:
            out[k] = v
    if item:
        out["item"] = item
    return out


def table_rows(
    session: Session,
    cons: Consensus,
    mode: RowsMode = RowsMode.ITEMS,
    include_unlabelled: bool = False,
) -> Iterator[dict[str, Any]]:
    """The label table. Item columns come after the label columns, prefixed ``item.``."""
    rules = session.rules
    with_scores = scored(rules)
    if mode is RowsMode.LABELS:
        for r in session.active:
            if r.skipped or r.decision is None or session.is_golden(r.item_id):
                continue
            it = session.items.get(r.item_id)
            if it is None:
                continue
            row = {
                "item_id": r.item_id,
                "queue": r.queue,
                "category": r.category,
                "label": r.decision,
                "label_display": rules.display(r.decision),
                **_rec_row(session, r),
                "record_id": r.record_id,
            }
            row.update({f"item.{k}": _flat(v) for k, v in _row_of(session, it).items()})
            yield row
        return
    ids: list[str] = list(cons.resolved)
    if include_unlabelled:
        ids += list(cons.conflicts) + list(cons.incomplete)
    for it in _items_in_order(session, ids):
        res = cons.resolved.get(it.id)
        if res is None:
            status = "conflict" if it.id in cons.conflicts else "incomplete"
            row = {"item_id": it.id, "queue": it.queue, "category": it.category, "status": status}
        else:
            recs = sorted(res.records, key=lambda r: r.labelled_at)
            last = recs[-1]
            approved, hl = table_scores(rules, res.decision)
            row = {
                "item_id": it.id,
                "queue": it.queue,
                "category": it.category,
                "label": res.decision,
                "label_display": rules.display(res.decision),
                "approved": approved,
                "human_label": hl,
                "status": res.status,
                "n_labels": len(res.votes or res.records),
                "labeller": ";".join(r.labeller for r in recs),
                "labelled_at": last.labelled_at,
                "note": " | ".join(r.note for r in recs if r.note) or None,
                "source": recs[0].source.value,
                "stage": recs[0].stage,
                "project_version": session.versions.get(last.record_id),
            }
        if not with_scores:
            row.pop("approved", None)
            row.pop("human_label", None)
        row.update({f"item.{k}": _flat(v) for k, v in _row_of(session, it).items()})
        yield row


def project_golden_rows(session: Session, cons: Consensus) -> Iterator[dict[str, Any]]:
    rules = session.rules
    for it in _items_in_order(session, cons.resolved):
        res = cons.resolved[it.id]
        row = copy.deepcopy(_row_of(session, it))
        approved = rules.approved(res.decision)
        recs = sorted(res.records, key=lambda r: r.labelled_at)
        row.update(
            label=rules.outcome(res.decision),
            label_approved=approved,
            label_source=recs[0].source.value,
            label_stage=recs[0].stage,
        )
        if not isinstance(row.get("metadata"), dict):
            row["metadata"] = {}
        last = recs[-1]
        row["metadata"]["labelling"] = {
            k: v
            for k, v in _provenance(res, session.versions.get(last.record_id)).items()
            if k not in ("label_source", "label_stage")
        }
        try:
            GoldenRow.model_validate(row)
        except ValueError:
            raise ExportError(
                f"item {it.id} is not a GoldenRow (golden export needs id + request columns)",
                "add the GoldenRow columns to the card as hidden fields, or export rollouts/csv",
            ) from None
        yield row


def project_rollout_rows(session: Session, cons: Consensus) -> Iterator[dict[str, Any]]:
    from caliper_sdk import EvalRow

    rules = session.rules
    for it in _items_in_order(session, cons.resolved):
        res = cons.resolved[it.id]
        hl = rules.human_label(res.decision)
        if hl is None:
            raise ExportError(
                "rollouts need a numeric human label; free-text answers have none",
                "export csv, jsonl or records instead",
            )
        raw = _row_of(session, it)
        if isinstance(raw.get("rollout"), dict):
            row = copy.deepcopy(raw)
            meta = row["rollout"].setdefault("metadata", {})
        else:
            rs = getattr(session, "rollout_cfg", None)  # ProjectConfig.rollout (dotted paths)
            prompt = _text(get_path(raw, rs.prompt)) if rs and rs.prompt else None
            if prompt is None:
                req = raw.get("request")
                if isinstance(req, dict):
                    prompt = _render_request(req)
                else:
                    prompt = "\n".join(
                        f"{f['label']}: {f['value']}"
                        for f in session.items.card(it)
                        if f.get("value") is not None and f["type"] != "image"
                    )
            response = _text(get_path(raw, rs.response)) if rs and rs.response else None
            if response is None:
                response = _text(raw.get("reference_outcome") or raw.get("response")) or ""
            gold = _text(get_path(raw, rs.gold)) if rs and rs.gold else None
            meta = (
                copy.deepcopy(raw.get("metadata")) if isinstance(raw.get("metadata"), dict) else {}
            )
            meta.setdefault("queue", it.queue)
            if it.category is not None:
                meta.setdefault("category", it.category)
            row = {
                "id": it.id,
                "rollout": {"prompt": prompt, "response": response, "gold": gold, "metadata": meta},
            }
        meta["human_label"] = _human_label(hl)
        last = sorted(res.records, key=lambda r: r.labelled_at)[-1]
        prov = _provenance(res, session.versions.get(last.record_id))
        meta.update({k: v for k, v in prov.items() if v is not None})
        yield EvalRow.model_validate(row).model_dump(mode="json")


def session_rows(
    session: Session,
    fmt: ExportFormat,
    policy: ConflictPolicy = ConflictPolicy.EXCLUDE,
    *,
    rows: RowsMode = RowsMode.ITEMS,
    include_unlabelled: bool = False,
) -> tuple[Iterator[dict[str, Any]] | list[dict[str, Any]], Consensus]:
    """Rows for any format, from a Session (legacy or project)."""
    legacy = session.dataset is not None
    if legacy:
        cons = consensus(session.dataset, session.config, session.records, policy)
    else:
        cons = session_consensus(session, policy)
    if fmt is ExportFormat.RECORDS:
        return [r.model_dump(mode="json") for r in session.records], cons
    if fmt is ExportFormat.GOLDEN:
        if legacy:
            return golden_rows(session.dataset, session.config, cons, include_unlabelled), cons
        return project_golden_rows(session, cons), cons
    if fmt is ExportFormat.ROLLOUTS:
        if legacy:
            return rollout_rows(session.dataset, session.config, cons), cons
        return project_rollout_rows(session, cons), cons
    table = table_rows(session, cons, rows, include_unlabelled)
    if fmt is ExportFormat.JSONL:
        parse = json_columns(session)
        return (nest_item(r, parse) for r in table), cons
    return table, cons


# --------------------------------------------------------------------------- writing


def write_jsonl(rows: Iterable[dict[str, Any]], out_path: str | Path) -> int:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    return n


def csv_text(rows: Iterable[dict[str, Any]]) -> tuple[str, int]:
    rows = list(rows)
    cols: dict[str, None] = {}
    for r in rows:
        for k in r:
            cols.setdefault(k, None)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(cols), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: _flat(v) for k, v in r.items()})
    return buf.getvalue(), len(rows)


def write_local(rows: Iterable[dict[str, Any]], fmt: ExportFormat, out_path: str | Path) -> int:
    """Write a local file. Parquet goes through dev 1's sinks (DuckDB)."""
    p = Path(out_path)
    if fmt is ExportFormat.CSV:
        text, n = csv_text(rows)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return n
    if fmt is ExportFormat.PARQUET:
        return write_via_sinks(
            rows, {"kind": "file", "path": str(p), "format": "parquet"}, "parquet"
        )
    return write_jsonl(rows, p)


def write_via_sinks(
    rows: Iterable[dict[str, Any]],
    target: Any,
    fmt: str,
    *,
    project_dir: str | Path | None = None,
    overwrite: bool = False,
) -> int:
    """Parquet files, S3 upload and DB write-back go through ``caliper_label.sinks`` (dev 1).

    golden / rollouts / records / jsonl are JSON lines; csv, parquet and tables store
    nested values as JSON text. An existing S3 object is replaced only with ``overwrite``;
    a table is created if missing, otherwise appended to.
    """
    from . import sinks
    from .project import SourceConfig

    tgt = target if isinstance(target, SourceConfig) else SourceConfig.model_validate(target)
    file_fmt = {"golden": "jsonl", "rollouts": "jsonl", "records": "jsonl"}.get(fmt, fmt)
    res = sinks.write(
        rows,
        tgt,
        None if fmt == "table" else file_fmt,
        project_dir=project_dir,
        overwrite=overwrite,
    )
    return int(res.rows)


def is_remote(dest: str) -> bool:
    return "://" in dest
