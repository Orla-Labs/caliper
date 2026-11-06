"""Inter-rater agreement: how much do human labellers agree with each other?

Input is one rating per (item, rater): ``(item_id, rater, label[, category])``
as a :class:`RaterLabel`, a plain tuple, or a mapping. Labels are
categorical strings, so agreement is unweighted.

Method choice (``method="auto"``, the default)
----------------------------------------------
The method is decided **once, on the whole overlap set**, and then applied to
every category, so the per-category numbers are comparable with the overall
one:

* **Cohen's kappa** when exactly two distinct raters appear across the items
  rated by 2+ raters. Cohen's treats the raters as fixed people (rater A vs
  rater B), which is only meaningful when the same two people labelled every
  overlap item.
* **Fleiss' kappa** otherwise (three or more raters). Labelling tools usually
  rotate pairs (alice+bob on one item, bob+carol on another), so raters are
  interchangeable. We use the Fleiss formulation with a per-item rater count
  ``n_i`` (each item's observed agreement is ``sum_j n_ij (n_ij - 1) /
  (n_i (n_i - 1))``, averaged over items; chance agreement is
  ``sum_j p_j**2`` from the pooled label proportions). With a constant number
  of raters per item this is exactly Fleiss (1971).

``method="cohen"`` or ``method="fleiss"`` forces a method (Cohen's raises
``ValueError`` when more than two raters appear). For every method the report
also carries ``pairs``: Cohen's kappa for each rater pair on the items both
rated, which shows *who* disagrees with whom.

Undefined cases follow :func:`caliper_kappa.compute_kappa`: ``kappa=0.0``,
``kappa_undefined=True`` and an ``undefined_reason`` (no item rated by 2+
raters; every rating uses the same label, so chance agreement is 1).

Duplicates: when the same rater rates the same item more than once, the
**last** rating in input order wins. (The ``LabelRecord`` adapter in
``caliper_eval.inter_rater`` resolves undo/skip/timestamps before calling
this.) Items rated by only one rater are counted but excluded from kappa.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._kappa import _kappa_from_confusion

InterRaterMethod = Literal["cohen", "fleiss"]

UNCATEGORIZED = "(none)"
"""Category key used in ``by_category`` for ratings without a category."""


class RaterLabel(BaseModel):
    """One rating: rater ``rater`` put item ``item_id`` in class ``label``."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    rater: str
    label: str
    category: str | None = None


class PairAgreement(BaseModel):
    """Cohen's kappa for one rater pair on the items both of them rated."""

    model_config = ConfigDict(extra="forbid")

    rater_a: str
    rater_b: str
    n_items: int
    observed_agreement: float
    kappa: float
    kappa_undefined: bool
    undefined_reason: str | None = None


class AgreementStats(BaseModel):
    """Agreement over one set of overlap items (overall, or one category).

    * ``n_items`` - items rated by 2+ raters (the kappa denominator).
    * ``n_ratings`` - ratings on those items.
    * ``observed_agreement`` - the kappa's observed agreement: for Cohen's the
      fraction of items where both raters agree; for Fleiss' the mean
      pairwise agreement per item.
    * ``percent_all_agree`` - fraction of items where every rater gave the
      same label (the intuitive "agreement %").
    * ``label_counts`` - ratings per label (sorted by label).
    * ``confusion`` - Cohen's only: ``confusion[i][j]`` counts items where
      rater ``raters[0]`` said ``labels[i]`` and ``raters[1]`` said ``labels[j]``.
    """

    model_config = ConfigDict(extra="forbid")

    method: InterRaterMethod
    n_items: int
    n_raters: int
    n_ratings: int
    raters: list[str]
    labels: list[str]
    observed_agreement: float
    expected_agreement: float
    percent_all_agree: float
    kappa: float
    kappa_undefined: bool
    undefined_reason: str | None = None
    label_counts: dict[str, int] = Field(default_factory=dict)
    confusion: list[list[int]] | None = None


class InterRaterReport(BaseModel):
    """Inter-rater agreement, overall and by category."""

    model_config = ConfigDict(extra="forbid")

    method: InterRaterMethod
    n_items_total: int  # distinct items seen
    n_items_single_rated: int  # rated by one rater only; excluded from kappa
    n_duplicate_ratings: int  # same (item, rater) seen again; last one kept
    overall: AgreementStats
    by_category: dict[str, AgreementStats] = Field(default_factory=dict)
    pairs: list[PairAgreement] = Field(default_factory=list)
    category_conflicts: list[str] = Field(default_factory=list)  # item ids


RatingInput = RaterLabel | Sequence[Any] | Mapping[str, Any]


def _coerce(rating: RatingInput) -> RaterLabel:
    if isinstance(rating, RaterLabel):
        return rating
    if isinstance(rating, Mapping):
        rater = rating.get("rater", rating.get("labeller"))
        label = rating.get("label", rating.get("decision"))
        return RaterLabel(
            item_id=str(rating["item_id"]),
            rater=str(rater),
            label=str(label),
            category=rating.get("category"),
        )
    if isinstance(rating, Sequence) and not isinstance(rating, str) and len(rating) in (3, 4):
        category = rating[3] if len(rating) == 4 else None
        return RaterLabel(
            item_id=str(rating[0]),
            rater=str(rating[1]),
            label=str(rating[2]),
            category=None if category is None else str(category),
        )
    raise TypeError(
        "rating must be a RaterLabel, a (item_id, rater, label[, category]) tuple, "
        f"or a mapping; got {rating!r}"
    )


def _cohen(
    items: list[str],
    ratings: dict[str, dict[str, str]],
    rater_a: str,
    rater_b: str,
    labels: list[str],
) -> tuple[float, float, float, bool, list[list[int]]]:
    index = {lab: i for i, lab in enumerate(labels)}
    n = len(labels)
    confusion = [[0] * n for _ in range(n)]
    for item in items:
        confusion[index[ratings[item][rater_a]]][index[ratings[item][rater_b]]] += 1
    weights = [[0.0 if i == j else 1.0 for j in range(n)] for i in range(n)]
    kappa, p_o, p_e, undefined = _kappa_from_confusion(confusion, weights, len(items))
    return kappa, p_o, p_e, undefined, confusion


def _fleiss(
    items: list[str], ratings: dict[str, dict[str, str]], labels: list[str]
) -> tuple[float, float, float, bool]:
    total = 0
    pooled = dict.fromkeys(labels, 0)
    p_bar_sum = 0.0
    for item in items:
        counts: dict[str, int] = {}
        for lab in ratings[item].values():
            counts[lab] = counts.get(lab, 0) + 1
            pooled[lab] += 1
        n_i = len(ratings[item])
        total += n_i
        p_bar_sum += sum(c * (c - 1) for c in counts.values()) / (n_i * (n_i - 1))
    p_bar = p_bar_sum / len(items)
    p_e = sum((c / total) ** 2 for c in pooled.values())
    if 1.0 - p_e <= 1e-12:
        return 0.0, p_bar, p_e, True
    return (p_bar - p_e) / (1.0 - p_e), p_bar, p_e, False


def _undefined_reason(n_items: int, labels: list[str]) -> str:
    if n_items == 0:
        return "no item was rated by 2+ raters"
    if len(labels) <= 1:
        return "every rating uses the same label (chance agreement is 1)"
    return "chance agreement is 1"


def _stats(
    items: list[str],
    ratings: dict[str, dict[str, str]],
    method: InterRaterMethod,
    cohen_raters: tuple[str, str] | None,
) -> AgreementStats:
    raters = sorted({r for it in items for r in ratings[it]})
    label_counts: dict[str, int] = {}
    for it in items:
        for lab in ratings[it].values():
            label_counts[lab] = label_counts.get(lab, 0) + 1
    labels = sorted(label_counts)
    n_ratings = sum(label_counts.values())
    all_agree = sum(1 for it in items if len(set(ratings[it].values())) == 1)
    confusion: list[list[int]] | None = None

    if not items:
        kappa, p_o, p_e, undefined = 0.0, 0.0, 0.0, True
    elif method == "cohen":
        assert cohen_raters is not None
        kappa, p_o, p_e, undefined, confusion = _cohen(items, ratings, *cohen_raters, labels)
        raters = list(cohen_raters)
    else:
        kappa, p_o, p_e, undefined = _fleiss(items, ratings, labels)

    return AgreementStats(
        method=method,
        n_items=len(items),
        n_raters=len(raters),
        n_ratings=n_ratings,
        raters=raters,
        labels=labels,
        observed_agreement=p_o,
        expected_agreement=p_e,
        percent_all_agree=(all_agree / len(items)) if items else 0.0,
        kappa=kappa,
        kappa_undefined=undefined,
        undefined_reason=_undefined_reason(len(items), labels) if undefined else None,
        label_counts={lab: label_counts[lab] for lab in labels},
        confusion=confusion,
    )


def inter_rater_kappa(
    ratings: Iterable[RatingInput],
    *,
    method: Literal["auto", "cohen", "fleiss"] = "auto",
) -> InterRaterReport:
    """Agreement between raters, overall and by category.

    ``ratings`` holds one entry per (item, rater): a :class:`RaterLabel`, a
    ``(item_id, rater, label)`` / ``(item_id, rater, label, category)``
    tuple, or a mapping with ``item_id``, ``rater`` (or ``labeller``),
    ``label`` (or ``decision``) and an optional ``category``.

    An item's category is the first non-null category seen for it; items
    whose ratings disagree on the category are listed in
    ``category_conflicts``. Items without a category appear under
    ``by_category["(none)"]`` only when some other item has a category.

    Deterministic: raters, labels and categories are sorted.
    """
    if method not in ("auto", "cohen", "fleiss"):
        raise ValueError(f"method must be 'auto', 'cohen' or 'fleiss', got {method!r}")

    by_item: dict[str, dict[str, str]] = {}
    item_category: dict[str, str | None] = {}
    conflicts: set[str] = set()
    n_dupes = 0
    for raw in ratings:
        r = _coerce(raw)
        slot = by_item.setdefault(r.item_id, {})
        if r.rater in slot:
            n_dupes += 1
            del slot[r.rater]  # re-insert so "last wins" also fixes rater order
        slot[r.rater] = r.label
        if r.category is not None:
            seen = item_category.get(r.item_id)
            if seen is None:
                item_category[r.item_id] = r.category
            elif seen != r.category:
                conflicts.add(r.item_id)
        else:
            item_category.setdefault(r.item_id, None)

    items = sorted(it for it, rs in by_item.items() if len(rs) >= 2)
    overlap_raters = sorted({rater for it in items for rater in by_item[it]})

    chosen: InterRaterMethod
    if method == "auto":
        chosen = "cohen" if len(overlap_raters) == 2 else "fleiss"
    elif method == "cohen":
        if len(overlap_raters) > 2:
            raise ValueError(
                f"Cohen's kappa needs exactly 2 raters; got {len(overlap_raters)} "
                f"({', '.join(overlap_raters)}). Use method='fleiss' or 'auto'."
            )
        chosen = "cohen"
    else:
        chosen = "fleiss"

    cohen_raters: tuple[str, str] | None = None
    if chosen == "cohen" and len(overlap_raters) == 2:
        cohen_raters = (overlap_raters[0], overlap_raters[1])

    overall = _stats(items, by_item, chosen, cohen_raters)

    by_category: dict[str, AgreementStats] = {}
    if any(item_category.get(it) is not None for it in by_item):
        groups: dict[str, list[str]] = {}
        for it in items:
            groups.setdefault(item_category.get(it) or UNCATEGORIZED, []).append(it)
        all_cats = {item_category.get(it) or UNCATEGORIZED for it in by_item}
        for cat in sorted(all_cats):
            by_category[cat] = _stats(groups.get(cat, []), by_item, chosen, cohen_raters)

    pairs: list[PairAgreement] = []
    for a, b in combinations(overlap_raters, 2):
        shared = [it for it in items if a in by_item[it] and b in by_item[it]]
        if not shared:
            continue
        labels = sorted({by_item[it][x] for it in shared for x in (a, b)})
        kappa, p_o, _p_e, undefined, _conf = _cohen(shared, by_item, a, b, labels)
        pairs.append(
            PairAgreement(
                rater_a=a,
                rater_b=b,
                n_items=len(shared),
                observed_agreement=p_o,
                kappa=kappa,
                kappa_undefined=undefined,
                undefined_reason=_undefined_reason(len(shared), labels) if undefined else None,
            )
        )

    return InterRaterReport(
        method=chosen,
        n_items_total=len(by_item),
        n_items_single_rated=sum(1 for rs in by_item.values() if len(rs) == 1),
        n_duplicate_ratings=n_dupes,
        overall=overall,
        by_category=by_category,
        pairs=pairs,
        category_conflicts=sorted(conflicts),
    )


__all__ = [
    "UNCATEGORIZED",
    "AgreementStats",
    "InterRaterMethod",
    "InterRaterReport",
    "PairAgreement",
    "RaterLabel",
    "inter_rater_kappa",
]
