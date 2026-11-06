"""Inter-rater kappa: textbook values, method choice, categories, undefined cases."""

from __future__ import annotations

import pytest
from caliper_kappa import UNCATEGORIZED, RaterLabel, inter_rater_kappa


def _two_rater(cells: dict[tuple[str, str], int]) -> list[tuple[str, str, str]]:
    """Expand a 2x2 contingency table into (item, rater, label) tuples."""
    out: list[tuple[str, str, str]] = []
    n = 0
    for (a, b), count in sorted(cells.items()):
        for _ in range(count):
            item = f"i{n:03d}"
            out += [(item, "A", a), (item, "B", b)]
            n += 1
    return out


def _cohen_by_hand(yy: int, yn: int, ny: int, nn: int) -> tuple[float, float, float]:
    n = yy + yn + ny + nn
    p_o = (yy + nn) / n
    p_yes = ((yy + yn) / n) * ((yy + ny) / n)
    p_no = ((ny + nn) / n) * ((yn + nn) / n)
    p_e = p_yes + p_no
    return p_o, p_e, (p_o - p_e) / (1 - p_e)


@pytest.mark.parametrize(
    ("yy", "yn", "ny", "nn", "textbook"),
    [
        (20, 5, 10, 15, 0.4),  # p_o = 0.70, p_e = 0.50
        (45, 15, 25, 15, 0.1304),  # p_o = 0.60, p_e = 0.54
    ],
)
def test_cohen_textbook(yy: int, yn: int, ny: int, nn: int, textbook: float) -> None:
    cells = {("yes", "yes"): yy, ("yes", "no"): yn, ("no", "yes"): ny, ("no", "no"): nn}
    ratings = _two_rater(cells)
    rep = inter_rater_kappa(ratings)
    p_o, p_e, kappa = _cohen_by_hand(yy, yn, ny, nn)
    assert rep.method == "cohen"
    assert rep.overall.observed_agreement == pytest.approx(p_o)
    assert rep.overall.expected_agreement == pytest.approx(p_e)
    assert rep.overall.kappa == pytest.approx(kappa)
    assert rep.overall.kappa == pytest.approx(textbook, abs=1e-4)
    assert rep.overall.percent_all_agree == pytest.approx(p_o)
    assert not rep.overall.kappa_undefined
    # labels sorted: ["no", "yes"]; confusion[A][B]
    assert rep.overall.labels == ["no", "yes"]
    assert rep.overall.confusion == [[nn, ny], [yn, yy]]
    assert rep.overall.raters == ["A", "B"]


# Fleiss (1971) worked example: 10 items, 14 raters, 5 categories.
_FLEISS_TABLE = [
    [0, 0, 0, 0, 14],
    [0, 2, 6, 4, 2],
    [0, 0, 3, 5, 6],
    [0, 3, 9, 2, 0],
    [2, 2, 8, 1, 1],
    [7, 7, 0, 0, 0],
    [3, 2, 6, 3, 0],
    [2, 5, 3, 2, 2],
    [6, 5, 2, 1, 0],
    [0, 2, 2, 3, 7],
]


def _fleiss_ratings() -> list[tuple[str, str, str]]:
    out = []
    for i, row in enumerate(_FLEISS_TABLE):
        rater = 0
        for j, count in enumerate(row):
            for _ in range(count):
                out.append((f"item{i}", f"r{rater:02d}", f"c{j}"))
                rater += 1
    return out


def test_fleiss_textbook() -> None:
    n_raters = 14
    p_i = [
        (sum(c * c for c in row) - n_raters) / (n_raters * (n_raters - 1)) for row in _FLEISS_TABLE
    ]
    p_bar = sum(p_i) / len(p_i)
    total = n_raters * len(_FLEISS_TABLE)
    p_j = [sum(row[j] for row in _FLEISS_TABLE) / total for j in range(5)]
    p_e = sum(p * p for p in p_j)
    expected = (p_bar - p_e) / (1 - p_e)

    rep = inter_rater_kappa(_fleiss_ratings())
    assert rep.method == "fleiss"
    assert rep.overall.observed_agreement == pytest.approx(p_bar)
    assert rep.overall.expected_agreement == pytest.approx(p_e)
    assert rep.overall.kappa == pytest.approx(expected)
    assert rep.overall.kappa == pytest.approx(0.210, abs=1e-3)  # published value
    assert rep.overall.n_items == 10
    assert rep.overall.n_raters == 14
    assert rep.overall.n_ratings == 140
    assert rep.overall.percent_all_agree == pytest.approx(0.1)  # only item0 unanimous
    assert rep.overall.confusion is None


def test_fleiss_variable_raters_per_item() -> None:
    # Rotating pairs + one triple-labelled item.
    ratings = [
        ("a", "alice", "keep"), ("a", "bob", "keep"),
        ("b", "bob", "drop"), ("b", "carol", "keep"),
        ("c", "alice", "drop"), ("c", "carol", "drop"), ("c", "bob", "drop"),
    ]  # fmt: skip
    rep = inter_rater_kappa(ratings)
    assert rep.method == "fleiss"
    # P_i: a=1, b=0, c=1 -> P_bar = 2/3. Pooled: keep 3/7, drop 4/7.
    p_e = (3 / 7) ** 2 + (4 / 7) ** 2
    assert rep.overall.observed_agreement == pytest.approx(2 / 3)
    assert rep.overall.kappa == pytest.approx((2 / 3 - p_e) / (1 - p_e))
    assert [(p.rater_a, p.rater_b, p.n_items) for p in rep.pairs] == [
        ("alice", "bob", 2),
        ("alice", "carol", 1),
        ("bob", "carol", 2),
    ]


def test_method_chosen_once_for_all_categories() -> None:
    # Overall 3 raters -> Fleiss; category "x" has only alice+bob but must still be Fleiss.
    ratings = [
        ("1", "alice", "y", "x"), ("1", "bob", "y", "x"),
        ("2", "alice", "n", "x"), ("2", "bob", "y", "x"),
        ("3", "carol", "n", "z"), ("3", "bob", "n", "z"),
    ]  # fmt: skip
    rep = inter_rater_kappa(ratings)
    assert rep.method == "fleiss"
    assert {c: s.method for c, s in rep.by_category.items()} == {"x": "fleiss", "z": "fleiss"}


def test_by_category_counts_and_undefined() -> None:
    ratings = [
        RaterLabel(item_id="1", rater="A", label="dup", category="near_dup"),
        RaterLabel(item_id="1", rater="B", label="dup", category="near_dup"),
        RaterLabel(item_id="2", rater="A", label="dup", category="near_dup"),
        RaterLabel(item_id="2", rater="B", label="dup", category="near_dup"),
        RaterLabel(item_id="3", rater="A", label="dup", category="mention"),
        RaterLabel(item_id="3", rater="B", label="distinct", category="mention"),
        RaterLabel(item_id="4", rater="A", label="distinct", category="mention"),
        RaterLabel(item_id="4", rater="B", label="distinct", category="mention"),
        RaterLabel(item_id="5", rater="A", label="distinct", category="solo"),
        RaterLabel(item_id="6", rater="A", label="distinct"),
        RaterLabel(item_id="6", rater="B", label="dup"),
    ]
    rep = inter_rater_kappa(ratings)
    assert rep.method == "cohen"
    assert rep.n_items_total == 6
    assert rep.n_items_single_rated == 1
    assert list(rep.by_category) == [UNCATEGORIZED, "mention", "near_dup", "solo"]

    near = rep.by_category["near_dup"]
    assert near.n_items == 2 and near.percent_all_agree == 1.0
    assert near.kappa_undefined and near.kappa == 0.0
    assert near.undefined_reason is not None and "same label" in near.undefined_reason

    mention = rep.by_category["mention"]
    assert mention.n_items == 2
    assert mention.percent_all_agree == pytest.approx(0.5)
    assert mention.label_counts == {"distinct": 3, "dup": 1}
    assert not mention.kappa_undefined

    solo = rep.by_category["solo"]
    assert solo.n_items == 0 and solo.kappa_undefined
    assert solo.undefined_reason == "no item was rated by 2+ raters"

    assert rep.by_category[UNCATEGORIZED].n_items == 1


def test_no_category_means_no_breakdown() -> None:
    rep = inter_rater_kappa([("1", "A", "x"), ("1", "B", "x"), ("2", "A", "y"), ("2", "B", "y")])
    assert rep.by_category == {}
    assert rep.overall.kappa == pytest.approx(1.0)


def test_no_overlap_is_undefined() -> None:
    rep = inter_rater_kappa([("1", "A", "x"), ("2", "B", "y")])
    assert rep.overall.n_items == 0
    assert rep.overall.kappa_undefined
    assert rep.n_items_single_rated == 2
    assert rep.pairs == []


def test_empty_input() -> None:
    rep = inter_rater_kappa([])
    assert rep.overall.kappa_undefined and rep.n_items_total == 0


def test_duplicates_last_wins_and_mapping_input() -> None:
    rep = inter_rater_kappa(
        [
            {"item_id": "1", "labeller": "A", "decision": "x"},
            {"item_id": "1", "rater": "B", "label": "y"},
            {"item_id": "1", "rater": "B", "label": "x"},  # B changes mind
            {"item_id": "2", "rater": "A", "label": "y"},
            {"item_id": "2", "rater": "B", "label": "y"},
        ]
    )
    assert rep.n_duplicate_ratings == 1
    assert rep.overall.percent_all_agree == 1.0


def test_category_conflicts_reported() -> None:
    rep = inter_rater_kappa([("1", "A", "x", "c1"), ("1", "B", "x", "c2")])
    assert rep.category_conflicts == ["1"]
    assert list(rep.by_category) == ["c1"]


def test_forced_cohen_with_three_raters_raises() -> None:
    with pytest.raises(ValueError, match="exactly 2 raters"):
        inter_rater_kappa([("1", "A", "x"), ("1", "B", "x"), ("1", "C", "y")], method="cohen")


def test_forced_fleiss_two_raters_is_scotts_pi() -> None:
    cells = {("yes", "yes"): 20, ("yes", "no"): 5, ("no", "yes"): 10, ("no", "no"): 15}
    ratings = _two_rater(cells)
    rep = inter_rater_kappa(ratings, method="fleiss")
    # Fleiss with 2 raters == Scott's pi: pooled proportions.
    p_yes = (25 + 30) / 100
    p_e = p_yes**2 + (1 - p_yes) ** 2
    assert rep.overall.kappa == pytest.approx((0.7 - p_e) / (1 - p_e))


def test_bad_input_rejected() -> None:
    with pytest.raises(TypeError):
        inter_rater_kappa([("1", "A")])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        inter_rater_kappa([], method="krippendorff")  # type: ignore[arg-type]


def test_deterministic_output() -> None:
    ratings = _fleiss_ratings()
    a = inter_rater_kappa(ratings).model_dump_json()
    b = inter_rater_kappa(list(reversed(ratings))).model_dump_json()
    assert a == b
