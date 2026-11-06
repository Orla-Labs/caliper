"""Regression tests for the setup-side fixes from usability test 1 (lpf-a)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from caliper_label.project import CardField, DisplayType, load_project
from caliper_label.project_setup import default_display
from caliper_label.typedetect import detect_display

# --------------------------------------------------------------------------- #15 datetime


@pytest.mark.parametrize(
    ("dtype", "values"),
    [
        ("TIMESTAMP", ["2026-11-06 09:30:00"]),
        ("TIMESTAMP WITH TIME ZONE", ["2026-11-06T09:30:00+00:00"]),
        ("TIMESTAMPTZ", ["2026-11-06T09:30:00Z"]),
        ("TIMESTAMP_NS", [None]),
        ("DATE", ["2026-11-06"]),
        ("VARCHAR", ["2026-11-06T09:30:00Z", "2026-11-05T17:02:11.123+01:00"]),
        ("VARCHAR", ["2026-11-06", "2026-10-30"]),
        ("VARCHAR", ["2026-11-06 09:30", "2026-11-06 10:00:59"]),
        ("unknown", [datetime(2026, 11, 6, tzinfo=UTC), date(2026, 11, 5)]),
    ],
)
def test_detect_datetime(dtype: str, values: list[Any]) -> None:
    assert detect_display(dtype, values) is DisplayType.DATETIME
    assert default_display(dtype, values) is DisplayType.DATETIME


@pytest.mark.parametrize(
    "values",
    [["2026-11-06 and more"], ["11/06/2026"], ["20261106"], ["2026-11-06", "not a date"]],
)
def test_detect_datetime_needs_every_value_iso(values: list[str]) -> None:
    assert detect_display("VARCHAR", values) is not DisplayType.DATETIME


def test_datetime_field_round_trips(tmp_path) -> None:
    (tmp_path / "caliper.label.yaml").write_text(
        "version: 1\nname: t\nsource: {kind: file, path: d.csv}\n"
        "card:\n  fields:\n    - {column: created_at, type: datetime}\n"
        "labels:\n  options: [{value: a}, {value: b}]\n"
    )
    cfg = load_project(tmp_path)
    assert cfg.card.fields[0] == CardField(column="created_at", type=DisplayType.DATETIME)


# --------------------------------------------------------------------------- #2 default folder


@pytest.fixture
def setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from caliper_label.routes_setup import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CALIPER_LABEL_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("CALIPER_LABEL_PROJECTS", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.state.ctx = None
    with TestClient(app) as c:
        yield c


def _git_checkout(path: Path, worktree: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if worktree:  # `git worktree` checkouts have a .git *file*
        (path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    else:
        (path / ".git").mkdir()
    return path


@pytest.mark.parametrize("worktree", [False, True])
def test_default_parent_is_never_inside_the_servers_git_checkout(
    setup_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worktree: bool
) -> None:
    repo = _git_checkout(tmp_path / "app-checkout", worktree)
    monkeypatch.chdir(repo)
    res = setup_client.get("/api/projects").json()
    parent = Path(res["default_parent"])
    assert parent == tmp_path / "userhome" / "caliper-projects"
    assert repo not in (parent, *parent.parents)
    # a remembered parent inside the checkout is skipped as well
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prefs.json").write_text(json.dumps({"last_parent": str(repo / "sub")}))
    assert setup_client.get("/api/projects").json()["default_parent"] == str(parent)


def test_default_parent_env_and_last_parent(
    setup_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CALIPER_LABEL_PROJECTS", str(tmp_path / "team-projects"))
    assert setup_client.get("/api/projects").json()["default_parent"] == str(
        tmp_path / "team-projects"
    )
    (tmp_path / "data.csv").write_text("id,text\n1,a\n2,b\n")
    cfg = {
        "name": "P",
        "source": {"kind": "file", "path": str(tmp_path / "data.csv"), "id_column": "id"},
        "card": {"fields": [{"column": "text"}]},
        "labels": {"options": [{"value": "a", "hotkey": "1"}]},
    }
    target = tmp_path / "chosen" / "p"
    r = setup_client.post(
        "/api/setup/create", json={"dir": str(target), "config": cfg, "open": False}
    )
    assert r.status_code == 200, r.text
    # the parent the user picked last time wins next time
    assert setup_client.get("/api/projects").json()["default_parent"] == str(
        (tmp_path / "chosen").resolve()
    )


def test_check_dir_warns_about_git_and_existing_projects(
    setup_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    repo = _git_checkout(tmp_path / "repo", worktree=True)
    r = setup_client.post("/api/setup/check-dir", json={"dir": str(repo / "labels" / "x")}).json()
    assert r["git_root"] == str(repo.resolve())
    assert any("git checkout" in w for w in r["warnings"])
    free = setup_client.post("/api/setup/check-dir", json={"dir": str(tmp_path / "new")}).json()
    assert free["git_root"] is None and free["warnings"] == [] and not free["exists"]
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "caliper.label.yaml").write_text("version: 1\n")
    old = setup_client.post("/api/setup/check-dir", json={"dir": str(tmp_path / "old")}).json()
    assert old["has_project"] and "already holds a project" in old["warnings"][0]


# --------------------------------------------------------------------------- #10 colours


def test_priority_categories_get_an_ordered_ramp() -> None:
    from caliper_label.project_setup import SEVERITY_RAMP, template_labels

    opts = template_labels("categories", "Low\nMedium\nHigh\nUrgent").options
    assert [o.color for o in opts] == SEVERITY_RAMP[:4]
    # order on screen does not matter: severity does
    opts = template_labels("categories", "Urgent\nLow\nHigh").options
    assert [o.color for o in opts] == [SEVERITY_RAMP[3], SEVERITY_RAMP[0], SEVERITY_RAMP[1]]
    p = template_labels("categories", "P0\nP1\nP2").options
    assert p[0].color == SEVERITY_RAMP[3] and p[2].color == SEVERITY_RAMP[0]


def test_sentiment_and_plain_categories_colours() -> None:
    from caliper_label.project_setup import (
        BAD,
        CATEGORY_PALETTE,
        GOOD,
        NEUTRAL,
        UNSURE,
        template_labels,
    )

    opts = template_labels("categories", "positive\nneutral\nnegative\nmixed").options
    assert [o.color for o in opts] == [GOOD, NEUTRAL, BAD, UNSURE]
    plain = template_labels("categories", "Billing\nShipping\nReturns").options
    assert [o.color for o in plain] == CATEGORY_PALETTE[:3]
    assert not {o.color for o in plain} & {GOOD, BAD, UNSURE}
    cwu = template_labels("correct_wrong_unsure").options
    assert [o.color for o in cwu] == [GOOD, BAD, UNSURE]


# --------------------------------------------------------------------------- #8 positive


def test_positive_warning_only_when_eval_exports_are_used() -> None:
    from caliper_label.project import ProjectConfig
    from caliper_label.project_setup import validate_project

    base = {
        "name": "Tickets",
        "source": {"kind": "file", "path": "t.csv"},
        "labels": {"options": [{"value": "low", "hotkey": "1"}, {"value": "high", "hotkey": "2"}]},
    }
    msgs = [i.message for i in validate_project(ProjectConfig.model_validate(base))]
    assert not any("positive" in m for m in msgs)
    with_golden = {
        **base,
        "exports": [
            {"name": "g", "format": "golden", "destination": {"kind": "file", "path": "g.jsonl"}}
        ],
    }
    issues = validate_project(ProjectConfig.model_validate(with_golden))
    assert any("positive" in i.message and i.severity == "warning" for i in issues)


# --------------------------------------------------------------------------- #14 options from data


def test_suggested_values_come_back_in_severity_order(setup_client) -> None:
    r = setup_client.post(
        "/api/setup/templates/categories",
        json={"categories": "medium\nhigh\nlow", "natural_order": True},
    )
    opts = r.json()["labels"]["options"]
    assert [(o["value"], o["hotkey"]) for o in opts] == [
        ("low", "1"),
        ("medium", "2"),
        ("high", "3"),
    ]
    # pasted lists keep the order the user typed
    r = setup_client.post("/api/setup/templates/categories", json={"categories": "medium\nlow"})
    assert [o["value"] for o in r.json()["labels"]["options"]] == ["medium", "low"]
