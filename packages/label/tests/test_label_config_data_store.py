"""Config parsing (YAML/TOML, key rules), dataset detection, and the append-only stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caliper_eval.types import LabelRecord, LabelSource
from caliper_label.config import LabelConfig, get_path, load_label_config, render_template
from caliper_label.data import DataKind, load_dataset
from caliper_label.store import JsonlStore, SqliteStore, load_records, open_store

ROOT = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------------- config


def test_example_config_loads() -> None:
    cfg = load_label_config(ROOT / "examples/catalog_cleanup/label.yaml")
    assert [d.key for d in cfg.decisions] == ["1", "2", "3"]
    assert cfg.decision("correct").approved and not cfg.decision("wrong").approved
    assert cfg.queue_key == "stratum" and cfg.keys.undo == "backspace"
    assert cfg.source is LabelSource.INTERNAL_REVIEWER


def test_example_serves_with_no_setup() -> None:
    cfg = load_label_config(ROOT / "examples/catalog_cleanup/label.yaml")
    ds = load_dataset(ROOT / "examples/catalog_cleanup/golden.jsonl", cfg)
    assert ds.kind is DataKind.GOLDEN and len(ds.items) == 80
    assert set(ds.queues) == {"common", "mixed", "rare_code", "lookalike", "unlisted"}
    for it in ds.items:  # every card image exists
        assert (ds.path.parent / render_template(cfg.fields[0].template, it.raw)).is_file()


def test_yaml_and_toml_are_equivalent(tmp_path: Path) -> None:
    y = tmp_path / "label.yaml"
    y.write_text(
        "fields:\n  - {key: text}\ndecisions:\n"
        '  - {key: 1, value: "yes", approved: true}\n  - {key: 2, value: "no"}\n'
        "double_label: 2\nkeys: {undo: Backspace}\n"
    )
    t = tmp_path / "label.toml"
    t.write_text(
        'double_label = 2\nkeys = { undo = "Backspace" }\nfields = [{ key = "text" }]\n'
        'decisions = [{ key = "1", value = "yes", approved = true }, { key = "2", value = "no" }]\n'
    )
    a, b = load_label_config(y), load_label_config(t)
    assert a.model_dump() == b.model_dump()
    assert a.decisions[0].key == "1" and a.keys.undo == "backspace"


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"decisions": [{"key": "s", "value": "a"}]}, "bound twice"),
        ({"decisions": [{"key": "z", "value": "a"}]}, "reserved"),
        ({"decisions": [{"key": "?", "value": "a"}]}, "reserved"),
        ({"decisions": [{"key": "ab", "value": "a"}]}, "one character"),
        ({"decisions": [{"key": "1", "value": "a"}, {"key": "1", "value": "b"}]}, "bound twice"),
        ({"decisions": [{"key": "1", "value": "a"}, {"key": "2", "value": "a"}]}, "unique"),
        ({"approved": ["nope"]}, "unknown decision"),
        ({"fields": [{"type": "text"}]}, "exactly one of"),
        ({"fields": [{"key": "a", "template": "b"}]}, "exactly one of"),
        ({"double_label": 0}, "greater than or equal"),
        ({"source": "robot"}, "source"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_config_rejects(tmp_path: Path, patch: dict, msg: str, base_config: dict) -> None:
    p = tmp_path / "bad.yaml"
    import yaml

    p.write_text(yaml.safe_dump({**base_config, **patch}))
    with pytest.raises(ValueError, match=msg):
        load_label_config(p)


def test_yaml_boolean_value_gets_a_clear_error(tmp_path: Path) -> None:
    p = tmp_path / "label.yaml"
    p.write_text("fields: [{key: text}]\ndecisions: [{key: 1, value: yes}, {key: 2, value: 3}]\n")
    with pytest.raises(ValueError, match="quote it"):
        load_label_config(p)
    p.write_text("fields: [{key: text}]\ndecisions: [{key: 1, value: 1}, {key: 2, value: 3}]\n")
    assert [d.value for d in load_label_config(p).decisions] == ["1", "3"]


def test_uppercase_decision_key_is_normalised_and_collides(base_config: dict) -> None:
    with pytest.raises(ValueError, match="bound twice"):
        LabelConfig.model_validate({**base_config, "decisions": [{"key": "S", "value": "a"}]})


def test_approved_list_marks_decisions(base_config: dict) -> None:
    cfg = LabelConfig.model_validate(
        {
            **base_config,
            "decisions": [{"key": "1", "value": "a"}, {"key": "2", "value": "b"}],
            "approved": ["b"],
        }
    )
    assert [d.approved for d in cfg.decisions] == [False, True]
    assert [d.human_label for d in cfg.decisions] == [0.0, 1.0]


def test_paths_and_templates() -> None:
    row = {"a": {"b": [{"c": 1}, {"c": 2}]}, "id": "x/1"}
    assert get_path(row, "a.b.-1.c") == 2
    assert get_path(row, "a.b.5.c") is None and get_path(row, "a.zz", "d") == "d"
    assert render_template("img/{id}.png", row) == "img/x/1.png"
    assert render_template("img/{missing}.png", row) is None


# --------------------------------------------------------------------------- data


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_detects_evalrow_and_plain(tmp_path: Path, base_config: dict) -> None:
    cfg = LabelConfig.model_validate({**base_config, "queue_key": None})
    ev = _write(tmp_path / "ev.jsonl", [{"id": "r1", "rollout": {"prompt": "p", "response": "r"}}])
    assert load_dataset(ev, cfg).kind is DataKind.EVALROW
    plain = _write(tmp_path / "p.jsonl", [{"text": "a"}, {"text": "b"}])
    ds = load_dataset(plain, cfg)
    assert ds.kind is DataKind.PLAIN
    assert [it.id for it in ds.items] == ["row-0000", "row-0001"]
    assert ds.queues == ["all"]


def test_dataset_errors(tmp_path: Path, base_config: dict) -> None:
    cfg = LabelConfig.model_validate(base_config)
    with pytest.raises(ValueError, match="duplicate id"):
        load_dataset(_write(tmp_path / "d.jsonl", [{"id": 1}, {"id": 1}]), cfg)
    bad = tmp_path / "b.jsonl"
    bad.write_text('{"id": 1}\nnot json\n')
    with pytest.raises(ValueError, match=r"b\.jsonl:2"):
        load_dataset(bad, cfg)


def test_request_rows_that_are_not_golden_fall_back_to_plain(
    tmp_path: Path, base_config: dict, capsys: pytest.CaptureFixture
) -> None:
    cfg = LabelConfig.model_validate(base_config)
    ds = load_dataset(_write(tmp_path / "g.jsonl", [{"id": "1", "request": {}, "outcome": 1}]), cfg)
    assert ds.kind is DataKind.PLAIN
    assert "treating the file as plain rows" in capsys.readouterr().err
    logs = load_dataset(ROOT / "examples/catalog_cleanup/logs.jsonl", cfg)
    assert logs.kind is DataKind.PLAIN and len(logs.items) == 300


# --------------------------------------------------------------------------- store


def _rec(i: int, **kw) -> LabelRecord:
    base = dict(
        item_id=f"g{i}",
        decision="correct",
        labeller="ana",
        labelled_at=f"2026-11-06T12:00:0{i}Z",
        record_id=f"r{i}",
    )
    return LabelRecord(**{**base, **kw})


@pytest.mark.parametrize("name", ["labels.jsonl", "labels.sqlite"])
def test_store_roundtrip(tmp_path: Path, name: str) -> None:
    store = open_store(tmp_path / name)
    assert isinstance(store, JsonlStore if name.endswith("jsonl") else SqliteStore)
    recs = [_rec(0), _rec(1, decision=None, skipped=True), _rec(2, undoes="r0", decision=None)]
    for r in recs:
        store.append(r)
    store.close()
    assert load_records(tmp_path / name) == recs


def test_jsonl_ignores_torn_last_line(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text(_rec(0).model_dump_json() + "\n" + '{"item_id": "g1", "deci')
    assert [r.record_id for r in load_records(p)] == ["r0"]
    assert "incomplete last line" in capsys.readouterr().err


def test_jsonl_recovers_from_torn_tail_and_keeps_appending(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text(_rec(0).model_dump_json() + "\n" + '{"item_id": "g1", "deci')
    store = open_store(p)  # as `serve` does after a crash
    assert "dropping an incomplete last line" in capsys.readouterr().err
    assert [r.record_id for r in store.load()] == ["r0"]
    store.append(_rec(1))
    store.close()
    assert [r.record_id for r in load_records(p)] == ["r0", "r1"]  # and it reopens fine
    assert [r.record_id for r in load_records(p)] == ["r0", "r1"]


def test_jsonl_rejects_corrupt_middle_line(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text("garbage\n" + _rec(0).model_dump_json() + "\n")
    with pytest.raises(ValueError, match=":1:"):
        load_records(p)


def test_store_suffix_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.jsonl or \.sqlite"):
        open_store(tmp_path / "labels.csv")
