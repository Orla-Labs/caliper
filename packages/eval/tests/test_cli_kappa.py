"""`caliper kappa`: --labels (inter-rater) and --rollouts (grader vs human_label)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caliper_eval.cli import main
from caliper_eval.types import LabelRecord, LabelSource

REPO = Path(__file__).resolve().parents[3]


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _write_labels(path: Path) -> None:
    def rec(rid, item, who, decision, cat, **kw):
        return LabelRecord(
            record_id=rid, item_id=item, labeller=who, decision=decision,
            labelled_at="2026-11-06T12:00:00Z", category=cat, **kw,
        )  # fmt: skip

    recs = [
        rec("1", "a", "ana", "keep", "near_dup"),
        rec("2", "a", "ben", "keep", "near_dup"),
        rec("3", "b", "ana", "keep", "near_dup"),
        rec("4", "b", "ben", "drop", "near_dup"),
        rec("5", "c", "ana", "drop", "mention"),
        rec("6", "c", "ben", "drop", "mention"),
        rec("7", "d", "ana", "drop", "mention"),
        rec("8", "d", "ben", "drop", "mention"),
        rec("9", "e", "ana", "keep", "mention"),  # single-rated
        rec("10", "e", "bot", "drop", "mention", source=LabelSource.LLM_TEACHER),
    ]
    path.write_text("".join(r.model_dump_json() + "\n" for r in recs), encoding="utf-8")


def test_labels_by_category(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_labels(tmp_path / "labels.jsonl")
    code, out, _ = run(capsys, "kappa", "--labels", "labels.jsonl", "--by", "category")
    assert code == 0
    assert out == (
        "Caliper kappa: inter-rater agreement\n"
        "labels: labels.jsonl (10 records, 9 kept after undo/skip/latest; sources: human)\n"
        "method: cohen (2 raters: ana, ben)\n"
        "items:  5 total, 4 rated by 2+ raters, 1 single-rated (excluded)\n"
        "\n"
        "by category\n"
        "  overall   n=4    agree  75.0%  kappa +0.500\n"
        "  mention   n=2    agree 100.0%  kappa undefined "
        "(every rating uses the same label (chance agreement is 1))\n"
        "  near_dup  n=2    agree  50.0%  kappa +0.000\n"
    )
    _, out2, _ = run(capsys, "kappa", "--labels", "labels.jsonl", "--by", "category")
    assert out2 == out  # deterministic

    code, out, _ = run(capsys, "kappa", "--labels", "labels.jsonl", "--source", "all")
    assert code == 0
    assert "method: fleiss (3 raters: ana, ben, bot)" in out
    assert "pairs (Cohen's kappa on shared items)" in out
    assert "by category" not in out

    code, out, _ = run(capsys, "kappa", "--labels", "labels.jsonl", "--json")
    assert json.loads(out)["overall"]["n_items"] == 4


def test_labels_usage_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.jsonl").write_text("{}\n", encoding="utf-8")
    code, _, err = run(capsys, "kappa", "--labels", "bad.jsonl")
    assert code == 2 and "bad.jsonl:1" in err
    code, _, err = run(capsys, "kappa", "--labels", "missing.jsonl")
    assert code == 2
    _write_labels(tmp_path / "labels.jsonl")
    code, _, err = run(capsys, "kappa", "--labels", "labels.jsonl", "--grader", "x.py:g")
    assert code == 2 and "--grader only applies" in err
    code, _, err = run(capsys, "kappa", "--labels", "labels.jsonl", "--source", "robots")
    assert code == 2
    with pytest.raises(SystemExit):
        main(["kappa"])  # one of --labels / --rollouts is required
    capsys.readouterr()


def test_rollouts_grader_vs_human_label(monkeypatch, capsys):
    monkeypatch.chdir(REPO)
    args = [
        "kappa",
        "--rollouts", "examples/invoice_totals/labeled_rollouts.jsonl",
        "--grader", "examples/invoice_totals/graders.py:fixed_grader",
    ]  # fmt: skip
    code, out, err = run(capsys, *args)
    assert code == 0, err
    assert out.startswith("Caliper kappa: grader vs human labels\n")
    assert "grader:   fixed_grader v1.0.0" in out
    assert "kappa +0.868" in out
    assert str(REPO) not in out
    _, out2, _ = run(capsys, *args)
    assert out2 == out

    args[-1] = "examples/invoice_totals/graders.py:naive_grader"
    code, out_naive, _ = run(capsys, *args, "--json")
    rep = json.loads(out_naive)
    assert code == 0 and rep["kappa"] < 1.0 and rep["n_unlabeled"] == 0


def test_rollouts_unlabelled_rows_and_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "g.py").write_text(
        "from caliper_sdk import GraderResult\n"
        "def g(rollout):\n"
        "    return GraderResult(score=1.0 if rollout.response == 'ok' else 0.0)\n",
        encoding="utf-8",
    )
    rows = [
        {"id": "r1", "rollout": {"prompt": "p", "response": "ok", "metadata": {"human_label": 1}}},
        {"id": "r2", "rollout": {"prompt": "p", "response": "no", "metadata": {"human_label": 0}}},
        {"id": "r3", "rollout": {"prompt": "p", "response": "no"}},
    ]
    (tmp_path / "r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, out, _ = run(capsys, "kappa", "--rollouts", "r.jsonl", "--grader", "g.py:g")
    assert code == 0
    assert "(3 rows, 2 with human_label, 1 without)" in out
    assert "grader:   g.py:g\n" in out

    code, _, err = run(capsys, "kappa", "--rollouts", "r.jsonl")
    assert code == 2 and "needs --grader" in err
    code, _, err = run(capsys, "kappa", "--rollouts", "r.jsonl", "--grader", "g.py:nope")
    assert code == 2 and "nope" in err
