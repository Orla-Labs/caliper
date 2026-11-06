"""Export labels as GoldenRows, labeled rollouts (EvalRow) or the raw record log.

Consensus per item, from the active labels (skips never count):

* fewer than ``double_label`` labels  -> *incomplete*, left out;
* all labels agree                    -> resolved to that decision;
* labels disagree                     -> *conflict*. Policy ``exclude`` (default)
  leaves it out; ``majority`` takes a strict majority and leaves ties out.

Everything left out is listed in the summary so nothing disappears silently.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from caliper_eval.types import GoldenRow, LabelRecord

from .config import LabelConfig, get_path
from .data import DataKind, Dataset
from .state import fold


class ExportFormat(StrEnum):
    GOLDEN = "golden"
    ROLLOUTS = "rollouts"
    RECORDS = "records"


class ConflictPolicy(StrEnum):
    EXCLUDE = "exclude"
    MAJORITY = "majority"


@dataclass
class Resolved:
    decision: str
    records: list[LabelRecord]


@dataclass
class Consensus:
    resolved: dict[str, Resolved] = field(default_factory=dict)
    conflicts: dict[str, dict[str, str]] = field(default_factory=dict)  # item -> labeller->dec
    incomplete: dict[str, int] = field(default_factory=dict)  # item -> labels so far
    unlabelled: int = 0


def consensus(
    dataset: Dataset,
    config: LabelConfig,
    records: list[LabelRecord],
    policy: ConflictPolicy = ConflictPolicy.EXCLUDE,
) -> Consensus:
    states, _ = fold(records)
    out = Consensus()
    for item in dataset.items:
        st = states.get(item.id)
        labels = list(st.labels.values()) if st else []
        if not labels:
            out.unlabelled += 1
            continue
        if len(labels) < config.double_label:
            out.incomplete[item.id] = len(labels)
            continue
        counts = Counter(r.decision for r in labels)
        top, top_n = counts.most_common(1)[0]
        if len(counts) == 1:
            out.resolved[item.id] = Resolved(str(top), labels)
        elif policy is ConflictPolicy.MAJORITY and top_n * 2 > len(labels):
            out.resolved[item.id] = Resolved(str(top), [r for r in labels if r.decision == top])
        else:
            out.conflicts[item.id] = {r.labeller: str(r.decision) for r in labels}
    return out


def _human_label(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _provenance(config: LabelConfig, res: Resolved) -> dict[str, Any]:
    recs = sorted(res.records, key=lambda r: r.labelled_at)
    return {
        "human_decision": res.decision,
        "label_source": recs[0].source.value,
        "label_stage": recs[0].stage,
        "labellers": [r.labeller for r in recs],
        "labelled_at": recs[-1].labelled_at,
        "notes": [r.note for r in recs if r.note] or None,
    }


def golden_rows(
    dataset: Dataset, config: LabelConfig, cons: Consensus, include_unlabelled: bool = False
) -> list[dict[str, Any]]:
    if dataset.kind is not DataKind.GOLDEN:
        raise ValueError(
            f"--format golden needs GoldenRow input; {dataset.path} is {dataset.kind.value} "
            "rows (use --format rollouts or records)"
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
            k: v
            for k, v in _provenance(config, res).items()
            if k not in ("label_source", "label_stage")
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
        meta.update({k: v for k, v in _provenance(config, res).items() if v is not None})
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
    """Write ``out_path``; return (rows written, consensus) for the summary."""
    cons = consensus(dataset, config, records, policy)
    if fmt is ExportFormat.RECORDS:
        rows = [r.model_dump(mode="json") for r in records]
    elif fmt is ExportFormat.GOLDEN:
        rows = golden_rows(dataset, config, cons, include_unlabelled)
    else:
        rows = rollout_rows(dataset, config, cons)
    p = Path(out_path)
    if p.parent:
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows), cons
