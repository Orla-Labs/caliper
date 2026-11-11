"""Lossless round-trip of `caliper.label.yaml` (dev 2).

Property-style: hundreds of seeded random ProjectConfigs, including awkward strings (YAML 1.1
booleans, numbers-as-strings, colons, hashes, unicode, multi-line markdown).

* UI -> YAML -> UI is identical, and a second save is byte-identical.
* Editing an existing file (A -> B) loads back as B, and is stable when saved again.
* A YAML 1.1 reader (pyyaml) sees the same values in a fresh file.
* A heavily commented hand-written file keeps every comment, key order and quoting, and
  an edit changes only the lines it should.
"""

from __future__ import annotations

import difflib
import os
import random
from pathlib import Path

import pytest
import yaml
from caliper_eval.types import LabelSource
from caliper_label import project as P
from caliper_label.project import (
    Assignment,
    CardConfig,
    CardField,
    Credentials,
    DisplayType,
    ExportTarget,
    GoldenCheck,
    Keys,
    LabelOption,
    LabelsConfig,
    LabelType,
    ProjectConfig,
    QueueConfig,
    RolloutConfig,
    ScoreRange,
    SourceConfig,
    SourceKind,
    WorkflowConfig,
    load_project,
    parse_project,
    render_project,
    save_project,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hand_written.caliper.label.yaml"

AWKWARD = [
    "yes",
    "No",
    "on",
    "OFF",
    "true",
    "null",
    "~",
    "1",
    "007",
    "1e3",
    "1_000",
    "0x1F",
    "3.14",
    ".inf",
    "2026-11-06",
    "a: b",
    "#hash",
    "x #y",
    "- dash",
    "[list]",
    "{map}",
    "*star",
    "&amp",
    "!bang",
    "%pct",
    "@at",
    "`tick`",
    "'quoted'",
    '"dq"',
    "Café · naïve ✓",
    "日本語のラベル",
    "trailing space ",
    " leading",
    "tab\there",
    "back\\slash",
    "line\nbreak",
    "para one\n\npara two\n",
    "ends with newline\n",
    "x" * 140,
    "word " * 40 + "end",
    "Codes: CTX-100 general · 210 packaged food · 310 books/maps · 420 kids' clothing · "
    "430 adult clothing/footwear",
]


def _text(r: random.Random, simple: bool = False) -> str:
    if not simple and r.random() < 0.35:
        return r.choice(AWKWARD)
    return r.choice(["subject", "body", "Ticket body", "priority", "Model said", "score_x"]) + (
        str(r.randint(0, 99)) if r.random() < 0.5 else ""
    )


def _maybe(r: random.Random, fn, p: float = 0.5):
    return fn() if r.random() < p else None


def random_config(seed: int) -> ProjectConfig:
    r = random.Random(seed)
    kind = r.choice(list(SourceKind))
    src = SourceConfig(
        kind=kind,
        path=_maybe(r, lambda: r.choice(["data/t.csv", "s3://bkt/x/*.parquet", "db.sqlite"])),
        format=_maybe(r, lambda: r.choice(["csv", "jsonl", "parquet"]), 0.3),
        table=_maybe(r, lambda: _text(r, True), 0.3),
        query=_maybe(r, lambda: "select *\nfrom t\nwhere x = 'a: b'", 0.2),
        sidecar=_maybe(r, lambda: "side.csv", 0.1),
        id_column=_maybe(r, lambda: _text(r)),
        plugin=_maybe(r, lambda: "snowflake", 0.1),
        options=r.choice([{}, {"schema": "support"}, {"sslmode": "require", "n": 3, "yes": True}]),
        credentials=r.choice(
            [
                Credentials(),
                Credentials(env="DATABASE_URL"),
                Credentials(aws_profile="dev", region="eu-west-1", endpoint_url="http://m:9000"),
                Credentials(env_map={"user": "PGUSER", "password": "PGPASSWORD"}),
            ]
        ),
    )
    fields = []
    for i in range(r.randint(0, 6)):
        use_template = r.random() < 0.15
        fields.append(
            CardField(
                column=None if use_template else f"{_text(r)}{i}",
                template=f"cards/{{id}}-{i}.svg" if use_template else None,
                type=r.choice(list(DisplayType)),
                label=_maybe(r, lambda: _text(r)),
                decimals=_maybe(r, lambda: r.randint(0, 4), 0.2),
                hidden=r.random() < 0.1,
                mono=r.random() < 0.1,
            )
        )
    opts = []
    for i in range(r.randint(0, 9)):
        opts.append(
            LabelOption(
                value=f"{_text(r)}-{i}" if r.random() < 0.7 else r.choice(AWKWARD) + str(i),
                label=_maybe(r, lambda: _text(r)),
                hotkey=_maybe(r, lambda i=i: str((i + 1) % 10), 0.8),
                color=_maybe(r, lambda: r.choice(["#2e7d32", "#C62828", "#aabbcc"])),
                approved=r.random() < 0.3,
                requires_note=r.random() < 0.2,
                outcome=_maybe(r, lambda: _text(r), 0.2),
                score=_maybe(r, lambda: round(r.random(), 3), 0.2),
                retired=r.random() < 0.1,
            )
        )
    labels = LabelsConfig(
        type=r.choice(list(LabelType)),
        options=opts,
        score=_maybe(r, lambda: ScoreRange(min=0, max=r.choice([5, 10, 1.5]), step=0.5), 0.2),
        keys=r.choice([Keys(), Keys(skip="k", undo="delete", note="m")]),
        category_column=_maybe(r, lambda: _text(r), 0.3),
    )
    wf = WorkflowConfig(
        queues=QueueConfig(by=_maybe(r, lambda: r.choice(["stratum", "priority", "yes"]))),
        double_label=r.randint(1, 4),
        assignment=r.choice(list(Assignment)),
        instructions=_maybe(r, lambda: r.choice([*AWKWARD, "# Title\n\n- **one**\n- two"])),
        golden=_maybe(
            r, lambda: GoldenCheck(answer_column="gold", rate=r.choice([0.05, 0.2])), 0.3
        ),
        source=r.choice(list(LabelSource)),
        stage=_maybe(r, lambda: _text(r), 0.3),
    )
    exports = [
        ExportTarget(
            name=f"exp{i}",
            format=r.choice(["csv", "golden", "table"]),
            destination=SourceConfig(kind=SourceKind.FILE, path=f"out/{i}.csv"),
        )
        for i in range(r.randint(0, 2))
    ]
    return ProjectConfig(
        revision=r.randint(1, 5),
        name=_text(r),
        description=_maybe(r, lambda: _text(r), 0.3),
        source=src,
        card=CardConfig(fields=fields),
        labels=labels,
        workflow=wf,
        exports=exports,
        rollout=_maybe(r, lambda: RolloutConfig(prompt="request.messages.-1.content"), 0.2),
    )


def _load_text(text: str) -> ProjectConfig:
    return parse_project(P._plain(P._yaml().load(text)))


# CALIPER_RT_SEEDS=2000 uv run pytest packages/label/tests/test_setup_roundtrip.py for a long run
N = int(os.environ.get("CALIPER_RT_SEEDS", "150"))
SEEDS = range(N)


@pytest.mark.parametrize("seed", SEEDS)
def test_fresh_roundtrip_identical(seed: int) -> None:
    cfg = random_config(seed)
    text = render_project(cfg)
    assert _load_text(text) == cfg
    # saving the unchanged config onto its own file is byte-identical
    assert render_project(cfg, text) == text
    # a YAML 1.1 reader agrees on every value
    assert ProjectConfig.model_validate(yaml.safe_load(text)) == cfg


@pytest.mark.parametrize("seed", range(N))
def test_edit_roundtrip(seed: int) -> None:
    a, b = random_config(seed), random_config(seed + 10_000)
    text_a = render_project(a)
    text_b = render_project(b, text_a)
    assert _load_text(text_b) == b
    assert render_project(b, text_b) == text_b
    # and back again
    text_a2 = render_project(a, text_b)
    assert _load_text(text_a2) == a


@pytest.mark.parametrize("seed", range(max(N // 3, 1)))
def test_hand_written_survives_random_edits(seed: int) -> None:
    src = FIXTURE.read_text(encoding="utf-8")
    b = random_config(seed)
    out = render_project(b, src)
    assert _load_text(out) == b
    assert render_project(b, out) == out


def test_save_and_load_project_dir(tmp_path: Path) -> None:
    cfg = random_config(7)
    path = save_project(tmp_path, cfg)
    assert path == tmp_path / "caliper.label.yaml"
    assert load_project(tmp_path) == cfg
    assert (tmp_path / ".caliper-label" / ".gitignore").read_text() == "*\n"
    before = path.read_text()
    save_project(tmp_path, cfg)
    assert path.read_text() == before


# --------------------------------------------------------------------------- hand-written file


def _diff(a: str, b: str) -> list[str]:
    return [
        ln
        for ln in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]


def _comments(text: str) -> list[str]:
    return [ln[ln.index("#") :].strip() for ln in text.splitlines() if "# " in ln]


def test_hand_written_unchanged_is_byte_identical(tmp_path: Path) -> None:
    src = FIXTURE.read_text(encoding="utf-8")
    (tmp_path / "caliper.label.yaml").write_text(src, encoding="utf-8")
    cfg = load_project(tmp_path)
    save_project(tmp_path, cfg)
    assert (tmp_path / "caliper.label.yaml").read_text(encoding="utf-8") == src


def test_hand_written_values_loaded() -> None:
    cfg = load_project(FIXTURE)
    assert cfg.source.kind is SourceKind.POSTGRES
    assert cfg.source.credentials.env == "DATABASE_URL"
    assert [o.hotkey for o in cfg.labels.options] == ["1", "2", "3"]  # ints coerced
    assert cfg.card.fields[3].column == "attachments.0.url"
    assert cfg.workflow.instructions.startswith("Is the **queue**")


def test_hand_written_single_edit_touches_one_line() -> None:
    src = FIXTURE.read_text(encoding="utf-8")
    cfg = load_project(FIXTURE)
    cfg.labels.options[1].label = "Wrong team"
    out = render_project(cfg, src)
    assert _diff(src, out) == [
        "-    - {value: wrong, label: Wrong queue, hotkey: 2, color: '#c62828'}   # most common",
        "+    - {value: wrong, label: Wrong team, hotkey: 2, color: '#c62828'}    # most common",
    ]


def test_hand_written_many_edits_keep_comments_and_order() -> None:
    src = FIXTURE.read_text(encoding="utf-8")
    cfg = load_project(FIXTURE)
    cfg.revision = 2
    cfg.workflow.double_label = 1
    cfg.labels.options.append(LabelOption(value="spam", label="Spam", hotkey="4"))
    cfg.card.fields.reverse()
    cfg.workflow.golden = GoldenCheck(answer_column="gold_queue")
    cfg.workflow.instructions += "\n- New rule."
    out = render_project(cfg, src)
    assert _load_text(out) == cfg
    assert _comments(out) == _comments(src) or sorted(_comments(out)) == sorted(_comments(src))
    assert set(_comments(src)) <= set(_comments(out))
    # top-level key order unchanged; new keys land next to their schema neighbours
    keys = [ln.split(":")[0] for ln in out.splitlines() if ln and ln[0].isalpha()]
    assert keys == ["version", "revision", "name", "description", "source", "card", "labels",
                    "workflow"]  # fmt: skip
    # the section header that followed card.fields still precedes `labels:`
    lines = out.splitlines()
    assert (
        lines[lines.index("labels:") - 1].startswith("# ----")
        and "decisions" in (lines[lines.index("labels:") - 1])
    )
    # each field's comment travelled with it
    subj = next(ln for ln in lines if "column: subject" in ln)
    assert subj.endswith("# first line of the ticket")
    # quoting styles kept for untouched values
    assert 'color: "#2e7d32"' in out and "color: '#c62828'" in out
    assert '  - {value: spam, label: Spam, hotkey: "4"}' in out


def test_hand_written_delete_section_keeps_following_comment() -> None:
    src = FIXTURE.read_text(encoding="utf-8")
    cfg = load_project(FIXTURE)
    cfg.workflow.stage = None
    cfg.description = None
    out = render_project(cfg, src)
    assert "stage:" not in out and "description:" not in out
    assert out.rstrip().endswith("# trailing comment at the end of the file")
    assert "# shown in the top bar" in out
    assert _load_text(out) == cfg


def test_legacy_file_is_refused_with_hint(tmp_path: Path) -> None:
    (tmp_path / "caliper.label.yaml").write_text("title: x\ndecisions: []\nfields: []\n")
    with pytest.raises(P.ProjectFileError) as ei:
        load_project(tmp_path)
    assert "legacy" in str(ei.value) and ei.value.hint


def test_bad_yaml_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(P.ProjectFileError, match="no project config"):
        load_project(tmp_path)
    (tmp_path / "caliper.label.yaml").write_text("source: [unclosed\n")
    with pytest.raises(P.ProjectFileError, match="cannot parse"):
        load_project(tmp_path)
    (tmp_path / "caliper.label.yaml").write_text("source: {kind: ftp}\n")
    with pytest.raises(P.ProjectFileError, match=r"source\.kind"):
        load_project(tmp_path)
    (tmp_path / "caliper.label.yaml").write_text(
        "source: {kind: file, path: x.csv}\nlabels: {options: [{value: yes}]}\n"
    )
    assert load_project(tmp_path).labels.options[0].value == "yes"  # YAML 1.2: a string
    (tmp_path / "caliper.label.yaml").write_text(
        "source: {kind: file, path: x.csv}\nlabels: {options: [{value: true}]}\n"
    )
    with pytest.raises(P.ProjectFileError, match="quote it"):
        load_project(tmp_path)
