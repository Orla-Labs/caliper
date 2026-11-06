"""Templates, hotkey clashes, validation, label-option migrations and the legacy adapter (dev 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from caliper_label.config import load_label_config
from caliper_label.project import (
    CardConfig,
    CardField,
    DisplayType,
    Keys,
    LabelOption,
    LabelsConfig,
    LabelType,
    ProjectConfig,
    SourceConfig,
    SourceKind,
    load_project,
    render_project,
    save_project,
)
from caliper_label.project_setup import (
    PALETTE,
    TEMPLATES,
    auto_hotkeys,
    default_display,
    errors,
    from_legacy,
    hotkey_issues,
    load_legacy,
    plan_label_migration,
    template_labels,
    validate_project,
)

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "examples" / "catalog_cleanup"


def _cfg(**labels) -> ProjectConfig:
    return ProjectConfig(
        source=SourceConfig(kind=SourceKind.FILE, path="d.csv"),
        card=CardConfig(fields=[CardField(column="a")]),
        labels=LabelsConfig(**labels),
    )


# --------------------------------------------------------------------------- templates


@pytest.mark.parametrize("t", [t.id for t in TEMPLATES])
def test_templates_are_valid(t: str) -> None:
    labels = template_labels(t, "Billing\nShipping\n\n  Returns  \nOther")
    cfg = _cfg(**labels.model_dump())
    assert errors(validate_project(cfg)) == []
    keys = [o.hotkey for o in labels.options]
    assert keys == [str(i + 1) for i in range(len(keys))]
    assert all(o.color for o in labels.options)


def test_template_contents() -> None:
    cwu = template_labels("correct_wrong_unsure")
    assert [(o.value, o.approved, o.requires_note) for o in cwu.options] == [
        ("correct", True, False),
        ("wrong", False, False),
        ("unsure", False, True),
    ]
    assert [o.value for o in template_labels("true_uncertain_false").options] == [
        "true",
        "uncertain",
        "false",
    ]
    rating = template_labels("rating_1_5")
    assert [o.value for o in rating.options] == ["1", "2", "3", "4", "5"]
    assert [o.score for o in rating.options] == [0, 0.25, 0.5, 0.75, 1]
    assert [o.approved for o in rating.options] == [False, False, False, True, True]
    assert template_labels("yes_no_unsure").type is LabelType.YES_NO_UNSURE
    cats = template_labels("categories", "Billing\nbilling!\n\nÉtat civil\n")
    assert [(o.value, o.label) for o in cats.options] == [
        ("billing", "Billing"),
        ("billing_2", "billing!"),
        ("tat_civil", "État civil"),
    ]
    with pytest.raises(ValueError, match="unknown template"):
        template_labels("nope")


def test_category_list_beyond_nine_keys() -> None:
    labels = template_labels("categories", [f"c{i}" for i in range(12)])
    assert [o.hotkey for o in labels.options][-3:] == ["0", None, None]
    assert errors(validate_project(_cfg(**labels.model_dump()))) == []


def test_template_roundtrips_through_yaml() -> None:
    cfg = _cfg(**template_labels("true_uncertain_false").model_dump())
    text = render_project(cfg)
    assert 'value: "true"' in text  # YAML 1.1 readers would see a boolean otherwise
    import yaml

    assert ProjectConfig.model_validate(yaml.safe_load(text)) == cfg


def test_palette_is_aa_on_white() -> None:
    def lum(hex_: str) -> float:
        rgb = [int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    for c in PALETTE:
        assert (1.05) / (lum(c) + 0.05) >= 4.5, c


# --------------------------------------------------------------------------- hotkeys


def test_auto_hotkeys_skip_taken() -> None:
    opts = [LabelOption(value="a", hotkey="2"), LabelOption(value="b"), LabelOption(value="c")]
    assert [o.hotkey for o in auto_hotkeys(opts)] == ["2", "1", "3"]


@pytest.mark.parametrize(
    ("keys", "skip", "fragment"),
    [
        (["1", "1"], "s", "bound twice"),
        (["1", "S"], "s", "bound twice"),  # case-insensitive
        (["1", "n"], "s", "bound twice"),  # note key
        (["z"], "s", "reserved"),
        (["?"], "s", "reserved"),
        (["1"], "Tab", "reserved"),
        (["1", "space"], "s", "reserved"),
        (["ab"], "s", "not a single key"),
        (["backspace"], "s", "bound twice"),  # undo is backspace
    ],
)
def test_hotkey_clashes(keys: list[str], skip: str, fragment: str) -> None:
    labels = LabelsConfig(
        options=[LabelOption(value=f"v{i}", hotkey=k) for i, k in enumerate(keys)],
        keys=Keys(skip=skip),
    )
    issues = hotkey_issues(labels)
    assert issues and fragment in issues[0].message


def test_retired_options_do_not_clash() -> None:
    labels = LabelsConfig(
        options=[
            LabelOption(value="a", hotkey="1"),
            LabelOption(value="b", hotkey="1", retired=True),
        ]
    )
    assert hotkey_issues(labels) == []


# --------------------------------------------------------------------------- validation


def test_validate_source_rules() -> None:
    def v(**src):
        cfg = _cfg(options=[LabelOption(value="a", hotkey="1", approved=True)])
        cfg.source = SourceConfig(**src)
        return {i.path: i for i in validate_project(cfg)}

    assert "source.path" in v(kind="file")
    assert "source.table" in v(kind="postgres")
    assert "source.table" in v(kind="postgres", table="t", query="select 1")
    assert "source.table" not in v(kind="postgres", table="t")
    pw = v(kind="postgres", query="select 1", path="postgresql://ana:hunter2@db/x")
    assert "source.path" in pw and "env" in pw["source.path"].hint
    assert "source.options.password" in v(kind="mysql", table="t", options={"password": "x"})
    assert v(kind="s3", path="s3://bucket/x/*.parquet") == {}


def test_validate_labels_and_card() -> None:
    cfg = _cfg()
    cfg.card.fields = []
    paths = {i.path for i in validate_project(cfg)}
    assert {"card.fields", "labels.options"} <= paths
    cfg = _cfg(options=[LabelOption(value="a", hotkey="1"), LabelOption(value="a", hotkey="2")])
    by = {i.path: i for i in validate_project(cfg)}
    assert "used twice" in by["labels.options.1.value"].message
    assert by["labels.options"].severity == "warning"  # nothing approved
    free = _cfg(type=LabelType.FREE_TEXT)
    assert errors(validate_project(free)) == []


# --------------------------------------------------------------------------- migrations


def test_migration_rename_keeps_value() -> None:
    old = template_labels("correct_wrong_unsure")
    new = old.model_copy(deep=True)
    new.options[1].label = "Wrong code"
    plan = plan_label_migration(old, new, {"wrong": 4})
    assert [(c.kind, c.value, c.labels) for c in plan.changes] == [("renamed", "wrong", 4)]
    assert not plan.needs_confirmation
    assert plan.labels.options[1].value == "wrong"


def test_migration_remove_with_labels_retires() -> None:
    old = template_labels("correct_wrong_unsure")
    new = old.model_copy(deep=True)
    new.options = [o for o in new.options if o.value != "unsure"]
    new.options.append(LabelOption(value="spam", label="Spam", hotkey="3"))
    plan = plan_label_migration(old, new, {"unsure": 2, "correct": 9})
    kinds = {c.value: c.kind for c in plan.changes}
    assert kinds == {"unsure": "retired", "spam": "added"}
    assert plan.needs_confirmation
    retired = [o for o in plan.labels.options if o.retired]
    assert [(o.value, o.hotkey) for o in retired] == [("unsure", None)]
    # hotkey 3 is free again: no clash with the retired option
    assert errors(validate_project(_cfg(**plan.labels.model_dump()))) == []


def test_migration_remove_without_labels_deletes() -> None:
    old = template_labels("correct_wrong_unsure")
    new = old.model_copy(update={"options": old.options[:2]})
    plan = plan_label_migration(old, new, {})
    assert [(c.kind, c.value) for c in plan.changes] == [("deleted", "unsure")]
    assert not plan.needs_confirmation
    assert [o.value for o in plan.labels.options] == ["correct", "wrong"]


def test_migration_restore_and_unknown_values() -> None:
    old = template_labels("correct_wrong_unsure")
    old.options[2] = old.options[2].model_copy(update={"retired": True, "hotkey": None})
    new = old.model_copy(deep=True)
    new.options[2].retired = False
    new.options[2].hotkey = "3"
    plan = plan_label_migration(old, new, {"unsure": 1, "ghost": 3})
    kinds = {(c.value, c.kind) for c in plan.changes}
    assert ("unsure", "restored") in kinds and ("ghost", "retired") in kinds


# --------------------------------------------------------------------------- legacy adapter


def test_legacy_catalog_adapter_field_by_field() -> None:
    legacy = load_label_config(CATALOG / "label.yaml")
    cfg = from_legacy(legacy, CATALOG / "golden.jsonl", project_dir=CATALOG)
    assert cfg.name == "Catalog cleanup · tax-code review"
    assert cfg.source.kind is SourceKind.FILE
    assert cfg.source.path == "golden.jsonl"
    assert cfg.source.format == "jsonl"
    assert cfg.source.id_column == "id"
    assert [(f.column, f.template, f.type, f.label, f.decimals) for f in cfg.card.fields] == [
        (None, "cards/{id}.svg", DisplayType.IMAGE, "Product", None),
        ("metadata.production_tax_code", None, DisplayType.BADGE, "Production code", None),
        ("metadata.production_score", None, DisplayType.NUMBER, "Confidence", 2),
        ("reference_outcome", None, DisplayType.BADGE, "Outcome", None),
        ("metadata.supplier", None, DisplayType.BADGE, "Supplier", None),
        ("request.messages.-1.content", None, DisplayType.TEXT, "Listing", None),
    ]
    assert [(o.value, o.label, o.hotkey, o.approved, o.outcome) for o in cfg.labels.options] == [
        ("correct", "Code correct", "1", True, "auto_publish"),
        ("wrong", "Wrong code", "2", False, "needs_review"),
        ("unsure", "Unsure", "3", False, "needs_review"),
    ]
    assert cfg.labels.type is LabelType.SINGLE
    assert cfg.labels.keys == Keys(skip="s", undo="backspace", note="n")
    assert cfg.labels.category_column == "metadata.production_tax_code"
    assert cfg.workflow.queues.by == "stratum"
    assert cfg.workflow.double_label == 1
    assert cfg.workflow.source.value == "internal_reviewer"
    assert cfg.workflow.stage == "tax_code_review"
    assert cfg.workflow.instructions == legacy.instructions
    assert cfg.rollout is not None
    assert cfg.rollout.prompt == "request.messages.-1.content"
    assert cfg.rollout.response == "metadata.production_tax_code"
    assert errors(validate_project(cfg)) == []


def test_legacy_adapter_roundtrips_and_extras(tmp_path: Path) -> None:
    cfg = load_legacy(CATALOG / "label.yaml", CATALOG / "golden.jsonl", project_dir=tmp_path)
    assert Path(cfg.source.path).is_absolute()  # outside the project dir: absolute
    save_project(tmp_path, cfg)
    assert load_project(tmp_path) == cfg
    (tmp_path / "label.yaml").write_text(
        "fields: [{key: body, mono: true}]\n"
        "decisions: [{key: a, value: ok}, {key: b, value: bad}]\n"
        "approved: [ok]\nid_key: ticket_id\nimage_root: imgs\n"
    )
    cfg2 = load_legacy(tmp_path / "label.yaml", tmp_path / "d.jsonl", project_dir=tmp_path)
    assert cfg2.source.path == "d.jsonl"
    assert cfg2.source.id_column == "ticket_id"
    assert cfg2.source.options == {"image_root": "imgs"}
    assert cfg2.card.fields[0].mono is True
    assert [o.approved for o in cfg2.labels.options] == [True, False]
    assert cfg2.rollout is None


# --------------------------------------------------------------------------- display guess


@pytest.mark.parametrize(
    ("dtype", "sample", "want"),
    [
        ("BIGINT", [1, 2], DisplayType.NUMBER),
        ("VARCHAR", ["https://x.test/a.png", "s3://b/k.jpg"], DisplayType.IMAGE),
        ("VARCHAR", ["https://x.test/a", "http://y.test"], DisplayType.LINK),
        ("VARCHAR", ["x" * 200], DisplayType.LONG_TEXT),
        ("VARCHAR", ["p1", "p2", "p1", "p1", "p2", "p1", "p2", "p1"], DisplayType.BADGE),
        ("STRUCT(a INT)", [{"a": 1}], DisplayType.JSON),
        ("VARCHAR", ["alpha one", "beta two", "gamma three"], DisplayType.TEXT),
    ],
)
def test_default_display(dtype: str, sample: list, want: DisplayType) -> None:
    assert default_display(dtype, sample) is want
