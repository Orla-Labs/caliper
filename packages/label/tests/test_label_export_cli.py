"""Export formats, conflict policy, round-trip into caliper_kappa, and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from caliper_eval.cli import main
from caliper_eval.inter_rater import inter_rater_from_records
from caliper_eval.loader import load_golden
from caliper_eval.types import GoldenRow, LabelRecord
from caliper_kappa import LabeledRow, LabelSet, compute_kappa
from caliper_label.export import ConflictPolicy, ExportFormat, consensus, export
from caliper_label.state import Session
from caliper_sdk import EvalRow, GraderResult, Rollout


def read(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines()]


def label_all(s: Session, plan: dict[str, list[tuple[str, str]]]) -> None:
    for iid, labels in plan.items():
        for who, dec in labels:
            s.label(iid, who, dec)


def test_golden_export_fills_label_fields(make_session, tmp_path: Path) -> None:
    s = make_session()
    label_all(s, {"g0": [("ana", "correct")], "g2": [("ana", "wrong")]})
    s.label("g1", "ana", None, skip=True)
    out = tmp_path / "out.jsonl"
    n, cons = export(s.dataset, s.config, s.records, out, ExportFormat.GOLDEN)
    assert n == 2 and cons.unlabelled == 3  # skipped g1 is not a label
    rows = [GoldenRow.model_validate(r) for r in read(out)]
    g0, g2 = rows
    assert (g0.id, g0.label, g0.label_approved) == ("g0", "auto_publish", True)
    assert (g2.label, g2.label_approved) == ("needs_review", False)
    assert g0.label_source.value == "internal_reviewer" and g0.label_stage == "triage"
    assert g0.metadata["labelling"]["labellers"] == ["ana"]
    assert g0.request == s.dataset.by_id["g0"].raw["request"]  # request untouched
    assert load_golden(str(out))[0].id == "g0"  # eval's own loader reads it

    n, _ = export(s.dataset, s.config, s.records, out, ExportFormat.GOLDEN, include_unlabelled=True)
    assert n == 5 and read(out)[1] == s.dataset.by_id["g1"].raw


def test_golden_export_needs_golden_input(make_session, tmp_path: Path) -> None:
    s = make_session(rows=[{"id": "a", "text": "x"}], queue_key=None)
    s.label("a", "ana", "correct")
    with pytest.raises(ValueError, match="needs GoldenRow input"):
        export(s.dataset, s.config, s.records, tmp_path / "o.jsonl", ExportFormat.GOLDEN)


def test_undo_respected_in_export(make_session, tmp_path: Path) -> None:
    s = make_session()
    s.label("g0", "ana", "correct")
    s.undo("ana")
    s.label("g0", "ana", "wrong")
    _, cons = export(s.dataset, s.config, s.records, tmp_path / "o.jsonl", ExportFormat.GOLDEN)
    assert cons.resolved["g0"].decision == "wrong"


def test_double_label_conflict_policy(make_session, tmp_path: Path) -> None:
    s = make_session(double_label=2)
    label_all(
        s,
        {
            "g0": [("ana", "correct"), ("ben", "correct")],
            "g1": [("ana", "correct"), ("ben", "wrong")],
            "g2": [("ana", "wrong")],
        },
    )
    cons = consensus(s.dataset, s.config, s.records)
    assert set(cons.resolved) == {"g0"}
    assert cons.conflicts == {"g1": {"ana": "correct", "ben": "wrong"}}
    assert cons.incomplete == {"g2": 1} and cons.unlabelled == 2

    s.label("g1", "cy", "wrong")
    assert "g1" not in consensus(s.dataset, s.config, s.records).resolved
    maj = consensus(s.dataset, s.config, s.records, ConflictPolicy.MAJORITY)
    assert maj.resolved["g1"].decision == "wrong"
    assert [r.labeller for r in maj.resolved["g1"].records] == ["ben", "cy"]


def test_rollouts_export_from_golden_feeds_compute_kappa(make_session, tmp_path: Path) -> None:
    s = make_session(rollout={"prompt": "request.messages.-1.content", "response": "metadata.code"})
    label_all(
        s,
        {
            "g0": [("ana", "correct")],
            "g1": [("ana", "wrong")],
            "g2": [("ana", "unsure")],
            "g3": [("ana", "correct")],
        },
    )
    out = tmp_path / "rollouts.jsonl"
    n, _ = export(s.dataset, s.config, s.records, out, ExportFormat.ROLLOUTS)
    assert n == 4
    rows = [EvalRow.model_validate(r) for r in read(out)]
    meta = {r.id: r.rollout.metadata for r in rows}
    assert meta["g0"]["human_label"] == 1 and isinstance(meta["g0"]["human_label"], int)
    assert meta["g1"]["human_label"] == 0 and meta["g2"]["human_label"] == 0.5
    assert meta["g0"]["human_decision"] == "correct" and meta["g0"]["labellers"] == ["ana"]
    assert meta["g0"]["code"] == "CTX-100" and meta["g0"]["stratum"] == "common"
    assert rows[0].rollout.prompt == "Item: Moonwick widget 0"
    assert rows[0].rollout.response == "CTX-100"

    # exactly what examples/invoice_totals/check.py does with labeled_rollouts.jsonl
    labels = LabelSet(
        name="human",
        rows=[
            LabeledRow(row_id=r.id, label_score=float(r.rollout.metadata["human_label"]))
            for r in rows
        ],
    )

    def grader(rollout: Rollout) -> GraderResult:  # agrees with the human on approve/reject
        return GraderResult(score=1.0 if rollout.metadata["human_decision"] == "correct" else 0.0)

    # 3-bucket edges keep "unsure" (0.5) apart; the default [0, .5, 1] puts 0.5 with "pass"
    rep = compute_kappa(grader, rows, labels, bin_edges=[0.0, 0.25, 0.75, 1.0])
    assert rep.n_labeled == 4 and rep.n_scored == 4 and rep.kappa < 1.0
    binary = compute_kappa(grader, rows[:2] + rows[3:], labels)
    assert binary.n_scored == 3 and binary.kappa == pytest.approx(1.0)


def test_rollouts_export_from_evalrows_keeps_rollout(make_session, tmp_path: Path) -> None:
    rows = [
        {"id": f"r{i}", "rollout": {"prompt": f"p{i}", "response": "ok", "metadata": {"case": "x"}}}
        for i in range(2)
    ]
    s = make_session(rows=rows, queue_key=None)
    label_all(s, {"r0": [("ana", "correct")], "r1": [("ana", "wrong")]})
    out = tmp_path / "o.jsonl"
    export(s.dataset, s.config, s.records, out, ExportFormat.ROLLOUTS)
    got = read(out)
    assert got[0]["rollout"]["prompt"] == "p0" and got[0]["rollout"]["metadata"]["case"] == "x"
    assert [g["rollout"]["metadata"]["human_label"] for g in got] == [1, 0]


def test_rollouts_default_prompt_renders_request(make_session, tmp_path: Path) -> None:
    s = make_session()
    s.label("g0", "ana", "correct")
    out = tmp_path / "o.jsonl"
    export(s.dataset, s.config, s.records, out, ExportFormat.ROLLOUTS)
    r = read(out)[0]["rollout"]
    assert (
        r["prompt"].startswith("[system]\nYou assign tax codes") and "[user]\nItem:" in r["prompt"]
    )
    assert r["response"] == "auto_publish"  # reference_outcome by default


def test_records_export_and_inter_rater(make_session, tmp_path: Path) -> None:
    s = make_session(double_label=2)
    label_all(
        s,
        {
            "g0": [("ana", "correct"), ("ben", "correct")],
            "g1": [("ana", "wrong"), ("ben", "correct")],
        },
    )
    s.undo("ben")
    out = tmp_path / "records.jsonl"
    n, _ = export(s.dataset, s.config, s.records, out, ExportFormat.RECORDS)
    assert n == 5
    recs = [LabelRecord.model_validate(r) for r in read(out)]
    rep = inter_rater_from_records(recs)
    assert rep.overall.n_items == 1  # ben's g1 label was undone


# --------------------------------------------------------------------------- CLI


def test_cli_export(
    make_session, data_dir: Path, tmp_path: Path, base_config: dict, capsys: pytest.CaptureFixture
) -> None:
    cfg = tmp_path / "label.yaml"
    cfg.write_text(yaml.safe_dump({**base_config, "double_label": 2}))
    s = make_session(double_label=2)
    label_all(
        s,
        {
            "g0": [("ana", "correct"), ("ben", "correct")],
            "g1": [("ana", "correct"), ("ben", "wrong")],
            "g2": [("ana", "wrong")],
        },
    )
    s.store.close()
    out = tmp_path / "golden_out.jsonl"
    rc = main(
        [
            "label",
            "export",
            "--data",
            str(data_dir / "golden.jsonl"),
            "--config",
            str(cfg),
            "--store",
            str(tmp_path / "labels.jsonl"),
            "--out",
            str(out),
        ]
    )
    cap = capsys.readouterr()
    assert rc == 0
    assert f"wrote 1 golden row(s) to {out}" in cap.out
    assert "resolved 1, conflicts 1, incomplete 1, unlabelled 2" in cap.out
    assert "left out (conflict): g1: ana=correct, ben=wrong" in cap.err
    assert "left out (incomplete, 1/2 labels): g2" in cap.err

    rc = main(
        [
            "label",
            "export",
            "--data",
            str(data_dir / "golden.jsonl"),
            "--config",
            str(cfg),
            "--store",
            str(tmp_path / "labels.jsonl"),
            "--out",
            str(out),
            "--format",
            "rollouts",
        ]
    )
    assert rc == 0 and len(read(out)) == 1


def test_cli_usage_errors(
    data_dir: Path, tmp_path: Path, base_config: dict, capsys: pytest.CaptureFixture
) -> None:
    cfg = tmp_path / "label.yaml"
    cfg.write_text(yaml.safe_dump(base_config))
    common = ["--data", str(data_dir / "golden.jsonl"), "--config", str(cfg)]
    assert (
        main(
            [
                "label",
                "export",
                *common,
                "--store",
                str(tmp_path / "none.jsonl"),
                "--out",
                str(tmp_path / "o.jsonl"),
            ]
        )
        == 2
    )
    assert "no label store" in capsys.readouterr().err
    bad = tmp_path / "bad.yaml"
    bad.write_text("fields: []\n")
    assert (
        main(
            [
                "label",
                "serve",
                str(data_dir / "golden.jsonl"),
                "--config",
                str(bad),
                "--store",
                str(tmp_path / "s.jsonl"),
                "--no-browser",
            ]
        )
        == 2
    )
    assert "caliper label serve:" in capsys.readouterr().err


def test_cli_default_store_is_under_build() -> None:
    from caliper_label.cli import default_store

    assert default_store("examples/catalog_cleanup/golden.jsonl") == Path(
        "build/labels/golden.labels.jsonl"
    )


def test_cli_serve_wires_uvicorn(
    monkeypatch, data_dir: Path, tmp_path: Path, base_config: dict, capsys
) -> None:
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    cfg = tmp_path / "label.toml"
    cfg.write_text('fields = [{ key = "id" }]\ndecisions = [{ key = "1", value = "ok" }]\n')
    rc = main(
        [
            "label",
            "serve",
            str(data_dir / "golden.jsonl"),
            "--config",
            str(cfg),
            "--store",
            str(tmp_path / "s.jsonl"),
            "--no-browser",
            "--port",
            "8770",
            "--labeller",
            "ana",
        ]
    )
    assert rc == 0 and seen["port"] == 8770 and seen["host"] == "127.0.0.1"
    out = capsys.readouterr().out
    assert "5 items (golden), 1 queue(s), 0 complete" in out
    assert "open http://127.0.0.1:8770/" in out
