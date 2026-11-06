"""Inter-annotator agreement on double-labelled items.

Each item with at least two active labels contributes one pair: its first and
second label in time order (rater slots A and B, since different people label
different items). Cohen's kappa is computed over those pairs, overall and per
category (``category_key`` in the config).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from caliper_eval.types import LabelRecord

from .state import Session


def _cohen(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "kappa": None, "observed": None, "expected": None, "undefined": True}
    observed = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    expected = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if expected >= 1.0:
        return {
            "n": n,
            "kappa": None,
            "observed": observed,
            "expected": expected,
            "undefined": True,
        }
    kappa = (observed - expected) / (1.0 - expected)
    return {"n": n, "kappa": kappa, "observed": observed, "expected": expected, "undefined": False}


def label_pairs(session: Session) -> list[tuple[str, str | None, LabelRecord, LabelRecord]]:
    """(item_id, category, first, second) for every item with >= 2 active labels."""
    out = []
    for item in session.dataset.items:
        labels = sorted(session.state(item.id).labels.values(), key=lambda r: r.labelled_at)
        if len(labels) >= 2:
            out.append((item.id, item.category, labels[0], labels[1]))
    return out


def agreement(session: Session) -> dict[str, Any]:
    pairs = label_pairs(session)
    overall = _cohen([(a.decision or "", b.decision or "") for _, _, a, b in pairs])
    by_cat: dict[str, list[tuple[str, str]]] = {}
    for _, cat, a, b in pairs:
        by_cat.setdefault(cat or "(none)", []).append((a.decision or "", b.decision or ""))
    categories = [{"category": c, **_cohen(p)} for c, p in sorted(by_cat.items())]
    disagreements = [
        {
            "item_id": iid,
            "category": cat,
            "labels": [
                {"labeller": r.labeller, "decision": r.decision, "note": r.note} for r in (a, b)
            ],
        }
        for iid, cat, a, b in pairs
        if a.decision != b.decision
    ]
    return {
        "method": "cohen_kappa_first_two_labels",
        "double_label": session.needed,
        "overall": overall,
        "by_category": categories,
        "disagreements": disagreements,
    }
