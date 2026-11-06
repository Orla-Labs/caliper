"""Legacy `serve DATA.jsonl --config label.yaml` behind the Session interfaces.

This is *not* the legacy -> ProjectConfig adapter (dev 2 owns that). It only maps
what the labelling loop needs: the in-memory JSONL dataset becomes an
:class:`~caliper_label.state.ItemSource`, the JSONL/SQLite store a
:class:`~caliper_label.state.LabelLog`, and the decisions become label options.
"""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote

from caliper_eval.types import LabelRecord

from .config import FieldSpec, FieldType, LabelConfig, get_path, render_template
from .data import Dataset, Item
from .project import Keys, LabelOption, LabelsConfig, LabelType, WorkflowConfig
from .state import ItemView
from .store import LabelStore


def legacy_labels(config: LabelConfig) -> LabelsConfig:
    return LabelsConfig(
        type=LabelType.SINGLE,
        options=[
            LabelOption(
                value=d.value,
                label=d.label or d.value,
                hotkey=d.key,
                approved=d.approved,
                score=d.score,
                outcome=d.outcome,
            )
            for d in config.decisions
        ],
        keys=Keys(**config.keys.model_dump()),
        category_column=config.category_key,
    )


def legacy_workflow(config: LabelConfig) -> WorkflowConfig:
    return WorkflowConfig(
        double_label=config.double_label,
        instructions=config.instructions,
        source=config.source,
        stage=config.stage,
    )


def _view(item: Item) -> ItemView:
    return ItemView(
        id=item.id, ordinal=item.index, queue=item.queue, category=item.category, data=item
    )


class DatasetItems:
    """The whole JSONL file is already in memory (legacy behaviour); serve pages of it."""

    def __init__(self, dataset: Dataset, config: LabelConfig) -> None:
        self.dataset = dataset
        self.config = config
        self._by_queue: dict[str, list[Item]] = {}
        for it in dataset.items:
            self._by_queue.setdefault(it.queue, []).append(it)

    def queues(self) -> list[str]:
        return list(self.dataset.queues)

    def _list(self, queue: str | None) -> list[Item]:
        return self.dataset.items if queue is None else self._by_queue.get(queue, [])

    def count(self, queue: str | None = None) -> int:
        return len(self._list(queue))

    def page(self, offset: int, limit: int, queue: str | None = None) -> list[ItemView]:
        return [_view(it) for it in self._list(queue)[offset : offset + limit]]

    def get(self, item_id: str) -> ItemView | None:
        it = self.dataset.by_id.get(item_id)
        return _view(it) if it else None

    def golden_ids(self) -> list[str]:
        return []

    def card(self, item: ItemView) -> list[dict[str, Any]]:
        return render_card(item.data, self.config)


class StoreLog:
    """A legacy LabelStore as a LabelLog (no project versions)."""

    def __init__(self, store: LabelStore) -> None:
        self.store = store

    def load(self) -> list[tuple[LabelRecord, int | None]]:
        return [(r, None) for r in self.store.load()]

    def append(self, record: LabelRecord, project_version: int | None) -> None:
        self.store.append(record)


# --------------------------------------------------------------------------- card


def _is_url(v: str) -> bool:
    return v.startswith(("http://", "https://"))


def field_raw(spec: FieldSpec, row: dict[str, Any]) -> Any:
    if spec.template is not None:
        return render_template(spec.template, row)
    assert spec.key is not None
    return get_path(row, spec.key)


def _display(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, indent=2, sort_keys=True)


def render_card(item: Item, config: LabelConfig) -> list[dict[str, Any]]:
    """The fields of one card as JSON-safe display values (the client escapes them)."""
    out = []
    for n, spec in enumerate(config.fields):
        raw = field_raw(spec, item.raw)
        f: dict[str, Any] = {"type": spec.type.value, "label": spec.name, "mono": spec.mono}
        if raw is None or raw == "":
            f["value"] = None
        elif spec.type is FieldType.NUMBER:
            if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
                f["value"] = f"{raw:.{spec.decimals}f}" if spec.decimals is not None else str(raw)
            else:
                f["value"] = _display(raw)
        elif spec.type is FieldType.LINK:
            s = str(raw)
            f["value"] = s
            f["href"] = s if _is_url(s) else None
        elif spec.type is FieldType.IMAGE:
            s = str(raw)
            f["value"] = s
            f["src"] = s if _is_url(s) else f"/api/image/{quote(item.id, safe='')}/{n}"
        else:
            f["value"] = _display(raw)
        out.append(f)
    return out
