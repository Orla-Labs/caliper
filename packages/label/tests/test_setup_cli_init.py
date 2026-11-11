"""`caliper label init` (dev 2, cli_init.py), driven through its own argparse registration."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from caliper_label import cli_init
from caliper_label.project import DisplayType, LabelType, SourceKind, load_project

ROOT = Path(__file__).resolve().parents[3]
CSV = "id,subject,body,priority,gold\n" + "".join(
    f"r{i},Subject {i},{'long body ' * 20}{i},p{i % 2},{'correct' if i % 3 else ''}\n"
    for i in range(12)
)


def run(*argv: str) -> int:
    root = argparse.ArgumentParser(prog="caliper")
    sub = root.add_subparsers(dest="cmd")
    lab = sub.add_parser("label").add_subparsers(dest="label_command")
    cli_init.register(lab)
    args = root.parse_args(["label", "init", *argv])
    return args.func(args)


@pytest.fixture
def csv(tmp_path: Path) -> Path:
    p = tmp_path / "tickets.csv"
    p.write_text(CSV)
    return p


def test_init_from_csv_detects_fields(tmp_path: Path, csv: Path, capsys) -> None:
    proj = tmp_path / "triage_project"
    rc = run(str(proj), "--data", str(csv), "--id-column", "id", "--queue-by", "priority",
             "--golden-column", "gold", "--copy-data", "--snapshot")  # fmt: skip
    assert rc == 0, capsys.readouterr().err
    cfg = load_project(proj)
    assert cfg.name == "Triage Project"
    assert cfg.source.kind is SourceKind.FILE and cfg.source.path == "data/tickets.csv"
    assert [f.column for f in cfg.card.fields] == ["subject", "body"]
    assert cfg.card.fields[1].type is DisplayType.LONG_TEXT
    assert [o.value for o in cfg.labels.options] == ["correct", "wrong", "unsure"]
    assert cfg.workflow.queues.by == "priority"
    assert cfg.workflow.golden and cfg.workflow.golden.answer_column == "gold"
    out = capsys.readouterr().out
    assert "12 item(s) snapshotted" in out and f"caliper label {proj}" in out
    # refuses to overwrite
    assert run(str(proj), "--data", str(csv)) == 2
    assert "exists" in capsys.readouterr().err
    assert run(str(proj), "--data", str(csv), "--force", "--labels", "rating_1_5") == 0
    assert [o.value for o in load_project(proj).labels.options] == ["1", "2", "3", "4", "5"]


def test_init_explicit_fields_and_options(tmp_path: Path, csv: Path) -> None:
    proj = tmp_path / "p"
    instr = tmp_path / "how.md"
    instr.write_text("# How\n\n- be kind\n")
    rc = run(str(proj), "--data", str(csv), "--fields", "subject,body:long_text,priority:badge",
             "--options", "Billing,Shipping,Other", "--approved", "Billing", "--multi",
             "--instructions", f"@{instr}", "--double-label", "2",
             "--assignment", "round_robin")  # fmt: skip
    assert rc == 0
    cfg = load_project(proj)
    assert [(f.column, f.type) for f in cfg.card.fields] == [
        ("subject", DisplayType.TEXT),
        ("body", DisplayType.LONG_TEXT),
        ("priority", DisplayType.BADGE),
    ]
    assert cfg.labels.type is LabelType.MULTI
    assert [(o.value, o.hotkey, o.approved) for o in cfg.labels.options] == [
        ("billing", "1", True),
        ("shipping", "2", False),
        ("other", "3", False),
    ]
    assert cfg.workflow.instructions == "# How\n\n- be kind\n"
    assert cfg.workflow.double_label == 2
    assert Path(cfg.source.path).is_absolute()  # outside the project, not copied


def test_init_database_offline_and_secret_refusal(tmp_path: Path, capsys) -> None:
    proj = tmp_path / "db"
    rc = run(str(proj), "--kind", "postgres", "--table", "tickets", "--env", "DATABASE_URL",
             "--no-detect", "--fields", "subject,body:long_text", "--labels", "categories",
             "--categories", "Billing,Shipping")  # fmt: skip
    assert rc == 0, capsys.readouterr().err
    cfg = load_project(proj)
    assert cfg.source.kind is SourceKind.POSTGRES and cfg.source.credentials.env == "DATABASE_URL"
    assert [o.label for o in cfg.labels.options] == ["Billing", "Shipping"]
    rc = run(str(tmp_path / "x"), "--data", "postgresql://u:pw@h/db", "--table", "t", "--no-detect",
             "--fields", "a")  # fmt: skip
    assert rc == 2 and "--env DATABASE_URL" in capsys.readouterr().err
    assert not (tmp_path / "x" / "caliper.label.yaml").exists()


def test_init_errors_are_explained(tmp_path: Path, capsys) -> None:
    assert run(str(tmp_path / "a"), "--data", str(tmp_path / "missing.csv")) == 2
    assert "hint" in capsys.readouterr().err
    assert run(str(tmp_path / "b"), "--kind", "postgres", "--no-detect") == 2
    assert "--fields" in capsys.readouterr().err
    assert run(str(tmp_path / "c"), "--kind", "postgres", "--no-detect", "--fields", "a") == 2
    assert "table or a SQL query" in capsys.readouterr().err


def test_init_from_legacy(tmp_path: Path) -> None:
    cat = ROOT / "examples" / "catalog_cleanup"
    rc = run(str(tmp_path / "cat"), "--from-legacy", str(cat / "label.yaml"),
             "--data", str(cat / "golden.jsonl"), "--snapshot")  # fmt: skip
    assert rc == 0
    cfg = load_project(tmp_path / "cat")
    assert cfg.labels.options[0].outcome == "auto_publish"
    assert cfg.rollout and cfg.rollout.response == "metadata.production_tax_code"
