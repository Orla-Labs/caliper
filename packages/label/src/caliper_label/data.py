"""Load the dataset to label: GoldenRow JSONL, EvalRow JSONL or plain JSONL rows.

The kind is detected per file from the rows: every row has ``request`` -> golden,
every row has ``rollout`` -> evalrow, otherwise plain. The raw dict of each row is
kept so exports round-trip it unchanged apart from the label fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from caliper_eval.types import GoldenRow

from .config import LabelConfig, get_path


class DataKind(StrEnum):
    GOLDEN = "golden"
    EVALROW = "evalrow"
    PLAIN = "plain"


@dataclass
class Item:
    id: str
    index: int
    raw: dict[str, Any]
    queue: str
    category: str | None


@dataclass
class Dataset:
    path: Path
    kind: DataKind
    items: list[Item]
    by_id: dict[str, Item] = field(default_factory=dict)
    queues: list[str] = field(default_factory=list)  # in first-seen order

    def __post_init__(self) -> None:
        self.by_id = {it.id: it for it in self.items}
        seen: dict[str, None] = {}
        for it in self.items:
            seen.setdefault(it.queue, None)
        self.queues = list(seen)


DEFAULT_QUEUE = "all"


def _scalar(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        return ",".join(str(x) for x in v) if v else None
    return str(v)


def detect_kind(rows: list[dict[str, Any]]) -> DataKind:
    if rows and all(isinstance(r.get("request"), dict) for r in rows):
        return DataKind.GOLDEN
    if rows and all(isinstance(r.get("rollout"), dict) for r in rows):
        return DataKind.EVALROW
    return DataKind.PLAIN


def load_dataset(path: str | Path, config: LabelConfig) -> Dataset:
    p = Path(path)
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: not JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{n}: expected a JSON object per line")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no rows")
    kind = detect_kind(rows)
    if kind is DataKind.GOLDEN:
        for n, row in enumerate(rows, start=1):
            try:
                GoldenRow.model_validate(row)
            except ValueError as exc:
                msg = f"{path}: row {n} looks like a GoldenRow but is not: {exc}"
                raise ValueError(msg) from exc

    items: list[Item] = []
    seen: set[str] = set()
    width = max(4, len(str(len(rows))))
    for i, row in enumerate(rows):
        rid = _scalar(get_path(row, config.id_key)) or f"row-{i:0{width}d}"
        if rid in seen:
            raise ValueError(f"{path}: duplicate id '{rid}' (set id_key in the config)")
        seen.add(rid)
        queue = DEFAULT_QUEUE
        if config.queue_key:
            queue = _scalar(get_path(row, config.queue_key)) or "(none)"
        category = _scalar(get_path(row, config.category_key)) if config.category_key else None
        items.append(Item(id=rid, index=i, raw=row, queue=queue, category=category))
    return Dataset(path=p, kind=kind, items=items)
