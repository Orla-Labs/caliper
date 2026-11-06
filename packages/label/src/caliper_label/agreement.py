"""Inter-annotator agreement on double-labelled items.

Delegates to :func:`caliper_eval.inter_rater.inter_rater_from_records` (Cohen's
kappa when exactly two people labelled the overlap set, Fleiss' kappa for three
or more, overall and by category). Only human sources count: agreement with an
LLM teacher is teacher agreement, not inter-rater agreement. This module adds
the list of items whose labels disagree, for the agreement view.
"""

from __future__ import annotations

from typing import Any

from caliper_eval.inter_rater import inter_rater_from_records
from caliper_kappa import AgreementStats

from .state import Session


def _stats(s: AgreementStats) -> dict[str, Any]:
    return {
        "n_items": s.n_items,
        "n_raters": s.n_raters,
        "kappa": None if s.kappa_undefined else s.kappa,
        "undefined": s.kappa_undefined,
        "undefined_reason": s.undefined_reason,
        "percent_all_agree": s.percent_all_agree if s.n_items else None,
        "observed": s.observed_agreement,
        "expected": s.expected_agreement,
        "label_counts": s.label_counts,
    }


def agreement(session: Session) -> dict[str, Any]:
    known = session.dataset.by_id
    records = [r for r in session.records if r.item_id in known]
    rep = inter_rater_from_records(records)
    disagreements = []
    for item in session.dataset.items:
        labels = session.state(item.id).labels
        if len(labels) >= 2 and len({r.decision for r in labels.values()}) > 1:
            disagreements.append(
                {
                    "item_id": item.id,
                    "category": item.category,
                    "labels": [
                        {"labeller": r.labeller, "decision": r.decision, "note": r.note}
                        for r in sorted(labels.values(), key=lambda r: r.labelled_at)
                    ],
                }
            )
    return {
        "method": rep.method,
        "double_label": session.needed,
        "n_items_overlap": rep.overall.n_items,
        "n_items_single_rated": rep.n_items_single_rated,
        "overall": _stats(rep.overall),
        "by_category": [
            {"category": cat, **_stats(s)} for cat, s in sorted(rep.by_category.items())
        ],
        "pairs": [p.model_dump(mode="json") for p in rep.pairs],
        "disagreements": disagreements,
    }
