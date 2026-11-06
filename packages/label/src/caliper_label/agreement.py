"""Inter-annotator agreement, disagreements, throughput and golden accuracy.

Kappa delegates to :func:`caliper_eval.inter_rater.inter_rater_from_records`
(Cohen's kappa when exactly two people labelled the overlap set, Fleiss' kappa for
three or more, overall and by category). Only human *votes* count: labels from an
LLM teacher are teacher agreement, adjudications are a final decision rather than a
vote, and golden check items are a test, not data. Everything here works from the
labelled items only, never a scan of the whole snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from caliper_eval.inter_rater import inter_rater_from_records
from caliper_kappa import AgreementStats

from .project import LabelType
from .state import HUMAN_VOTE_SOURCES, Session, is_adjudication

ACTIVE_GAP = timedelta(minutes=5)  # a longer pause between two labels is a break


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


def _known(session: Session, item_id: str) -> bool:
    return session.items.get(item_id) is not None


def disagreements(session: Session, *, include_resolved: bool = True) -> list[dict[str, Any]]:
    """Items with 2+ votes that differ, in snapshot order."""
    rules = session.rules
    out = []
    for iid, st in session.states.items():
        if session.is_golden(iid) or len(st.labels) < 2:
            continue
        votes = [r for r in st.labels.values() if r.source in HUMAN_VOTE_SOURCES]
        if len(votes) < 2 or len({r.decision for r in votes}) < 2:
            continue
        if st.adjudication is not None and not include_resolved:
            continue
        item = session.items.get(iid)
        if item is None:
            continue
        adj = st.adjudication
        out.append(
            {
                "item_id": iid,
                "ordinal": item.ordinal,
                "queue": item.queue,
                "category": item.category,
                "labels": [
                    {
                        "labeller": r.labeller,
                        "decision": r.decision,
                        "display": rules.display(r.decision),
                        "note": r.note,
                        "labelled_at": r.labelled_at,
                    }
                    for r in sorted(votes, key=lambda r: r.labelled_at)
                ],
                "adjudication": None
                if adj is None
                else {
                    "adjudicator": adj.labeller,
                    "decision": adj.decision,
                    "display": rules.display(adj.decision),
                    "note": adj.note,
                    "labelled_at": adj.labelled_at,
                },
            }
        )
    out.sort(key=lambda d: d["ordinal"])
    return out


def agreement(session: Session) -> dict[str, Any]:
    records = [r for r in session.vote_records() if _known(session, r.item_id)]
    rep = inter_rater_from_records(records, sources=HUMAN_VOTE_SOURCES)
    out: dict[str, Any] = {
        "method": rep.method,
        "double_label": session.needed,
        "n_items_overlap": rep.overall.n_items,
        "n_items_single_rated": rep.n_items_single_rated,
        "overall": _stats(rep.overall),
        "by_category": [
            {"category": cat, **_stats(s)} for cat, s in sorted(rep.by_category.items())
        ],
        "pairs": [p.model_dump(mode="json") for p in rep.pairs],
        "disagreements": [
            {k: d[k] for k in ("item_id", "category", "labels")} for d in disagreements(session)
        ],
    }
    if session.rules.type is LabelType.FREE_TEXT:
        for s in [out["overall"], *out["by_category"]]:
            s.update(
                kappa=None,
                undefined=True,
                undefined_reason="kappa is not defined for free-text answers",
            )
    return out


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _rate(times: list[datetime]) -> float | None:
    """Labels per hour of active time (gaps over 5 minutes count as breaks)."""
    if len(times) < 2:
        return None
    times = sorted(times)
    active = sum((min(b - a, ACTIVE_GAP) for a, b in pairwise(times)), timedelta())
    secs = active.total_seconds()
    return round((len(times) - 1) * 3600 / secs, 1) if secs > 0 else None


def dashboard(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    gold = session.golden_accuracy()
    per: dict[str, dict[str, Any]] = {}
    per_queue_hour: dict[str, int] = {}
    for r in session.active:
        if is_adjudication(r):
            continue
        row = per.setdefault(r.labeller, {"labels": 0, "skips": 0, "times": [], "last_hour": 0})
        if r.skipped or r.decision is None:
            row["skips"] += 1
            continue
        if session.is_golden(r.item_id):
            continue
        row["labels"] += 1
        t = _ts(r.labelled_at)
        row["times"].append(t)
        if t >= hour_ago:
            row["last_hour"] += 1
            per_queue_hour[r.queue or "all"] = per_queue_hour.get(r.queue or "all", 0) + 1
    labellers = []
    for name in sorted(set(per) | set(gold)):
        row = per.get(name, {"labels": 0, "skips": 0, "times": [], "last_hour": 0})
        g = gold.get(name)
        labellers.append(
            {
                "name": name,
                "labels": row["labels"],
                "skips": row["skips"],
                "last_hour": row["last_hour"],
                "per_hour": _rate(row["times"]),
                "last_at": max(row["times"]).strftime("%Y-%m-%dT%H:%M:%SZ")
                if row["times"]
                else None,
                "golden": g,
            }
        )
    queues = session.queues(None)
    for q in queues:
        q["last_hour"] = per_queue_hour.get(q["name"], 0)
    dis = disagreements(session)
    agr = agreement(session)
    total = sum(q["total"] for q in queues)
    complete = sum(q["complete"] for q in queues)
    return {
        "totals": {
            "items": total,
            "complete": complete,
            "labels": sum(x["labels"] for x in labellers),
            "labellers": len(labellers),
            "disagreements": len(dis),
            "open_disagreements": sum(1 for d in dis if d["adjudication"] is None),
            "adjudicated": sum(1 for st in session.states.values() if st.adjudication),
            "golden_items": len(session._golden),
        },
        "double_label": session.needed,
        "label_type": session.rules.type.value,
        "labellers": labellers,
        "queues": queues,
        "agreement": {k: agr[k] for k in ("method", "n_items_overlap", "overall", "by_category")},
        "disagreements": dis[:50],
    }
