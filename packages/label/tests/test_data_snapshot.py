"""ProjectStore + snapshot take/diff/apply (labels survive removal; history; pagination)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from caliper_eval.types import LabelRecord
from caliper_label import snapshot
from caliper_label.connectors import ConnectorError, open_connector
from caliper_label.project import ProjectConfig
from caliper_label.project_store import ProjectStore, ensure_state_dir
from caliper_label.state import Session
from caliper_label.store import new_record_id, utc_now


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def rows(n: int, over: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        r = {
            "ticket_id": f"T-{i:04d}",
            "subject": f"Fictional subject {i}",
            "body": f"Body {i}",
            "priority": "high" if i % 2 else "low",
            "code": f"C{i % 3}",
            "answer": "correct" if i == 0 else None,
            "internal": f"not shown {i}",
        }
        r.update((over or {}).get(i, {}))
        out.append(r)
    return out


def make_ctx(tmp_path: Path, cfg: dict[str, Any] | None = None) -> SimpleNamespace:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    base = {
        "name": "Fictional triage",
        "source": {"kind": "file", "path": "tickets.jsonl", "id_column": "ticket_id"},
        "card": {
            "fields": [
                {"column": "subject"},
                {"column": "body", "type": "long_text"},
                {"template": "https://example.invalid/t/{ticket_id}", "type": "link"},
            ]
        },
        "labels": {"category_column": "code"},
        "workflow": {"queues": {"by": "priority"}, "golden": {"answer_column": "answer"}},
    }
    base.update(cfg or {})
    config = ProjectConfig.model_validate(base)
    store = ProjectStore.open(proj)
    return SimpleNamespace(
        project_dir=proj,
        config=config,
        store=store,
        connector=open_connector(config.source, proj),
    )


def label(item_id: str, who: str = "ana", decision: str = "correct") -> LabelRecord:
    fields = {"item_id": item_id, "labeller": who, "decision": decision}
    return LabelRecord(**fields, labelled_at=utc_now(), record_id=new_record_id(fields))


def test_state_dir_gitignore(tmp_path: Path) -> None:
    d = ensure_state_dir(tmp_path)
    assert (d / ".gitignore").read_text() == "*\n"
    assert d.name == ".caliper-label"


def test_take_snapshots_displayed_fields_and_metadata(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(10))
    info = snapshot.take(ctx, batch_size=3)
    assert info.kind == "take" and info.n_items == 10
    assert (ctx.project_dir / ".caliper-label" / "project.db").is_file()
    s = ctx.store
    assert s.count() == 10 and s.count("high") == 5
    assert s.queues() == [("low", 5), ("high", 5)]
    it = s.item("T-0000")
    assert it is not None
    assert it.fields == {"subject": "Fictional subject 0", "body": "Body 0", "ticket_id": "T-0000"}
    assert it.queue == "low" and it.category == "C0" and it.golden_answer == "correct"
    assert "internal" not in it.fields and "answer" not in it.fields  # golden stays hidden
    assert s.item("T-0001").golden_answer is None
    page = s.items(offset=2, limit=3)
    assert [i.item_id for i in page] == ["T-0002", "T-0003", "T-0004"]
    assert [i.item_id for i in s.items(limit=2, queue="high")] == ["T-0001", "T-0003"]
    assert [i.item_id for i in s.items(after=page[-1].ordinal, limit=1)] == ["T-0005"]
    assert len(list(s.iter_items(batch_size=4))) == 10
    # idempotent
    assert snapshot.take(ctx).id == info.id and s.count() == 10


def test_no_card_fields_keeps_all_columns_and_row_ids(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        {"card": {"fields": []}, "source": {"kind": "file", "path": "tickets.jsonl"}},
    )
    write_rows(ctx.project_dir / "tickets.jsonl", rows(3))
    snapshot.take(ctx)
    it = ctx.store.items(limit=1)[0]
    assert it.item_id == "row-0000" and "internal" in it.fields
    d = snapshot.diff(ctx)
    assert d.warning and "id column" in d.warning


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    data = rows(6, {4: {"ticket_id": "T-0001"}})
    write_rows(ctx.project_dir / "tickets.jsonl", data)
    with pytest.raises(ConnectorError) as e:
        snapshot.take(ctx, batch_size=2)
    assert "duplicate id 'T-0001'" in e.value.message and e.value.hint
    assert ctx.store.count() == 0 and not ctx.store.has_snapshot()  # rolled back


def test_missing_id_value_rejected(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(3, {2: {"ticket_id": None}}))
    with pytest.raises(ConnectorError) as e:
        snapshot.take(ctx)
    assert "row 3" in e.value.message


def test_refresh_diff_and_apply(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    path = ctx.project_dir / "tickets.jsonl"
    write_rows(path, rows(6))
    snapshot.take(ctx)
    ctx.store.append_label(label("T-0002"), project_version=1)
    ctx.store.append_label(label("T-0003", "bo"), project_version=1)

    # T-0002 removed (labelled), T-0003 removed, T-0001 subject changed, internal-only change on
    # T-0004 (not displayed -> not a change), two new rows.
    new = [r for r in rows(8) if r["ticket_id"] not in ("T-0002", "T-0003")]
    for r in new:
        if r["ticket_id"] == "T-0001":
            r["subject"] = "Edited subject"
        if r["ticket_id"] == "T-0004":
            r["internal"] = "edited but hidden"
    write_rows(path, new)
    d = snapshot.diff(ctx, sample=1)
    assert (d.n_new, d.n_removed, d.n_changed, d.n_unchanged) == (2, 2, 1, 3)
    assert d.new == ["T-0006"] and d.removed == ["T-0002"] and d.changed == ["T-0001"]
    assert d.removed_with_labels == 2 and d.warning is None
    assert snapshot.diff_ids(ctx.store, d.diff_id, "new", 1, 10) == ["T-0007"]
    assert ctx.store.count() == 6  # nothing applied yet

    info = snapshot.apply(ctx, d, note="weekly")
    assert info.kind == "refresh" and (info.n_new, info.n_removed, info.n_changed) == (2, 2, 1)
    assert info.n_items == 6 and info.note == "weekly"
    s = ctx.store
    assert s.count() == 6 and s.count(removed_only=True) == 2
    gone = s.item("T-0002")
    assert gone is not None and gone.removed
    assert [r.item_id for r in s.labels()] == ["T-0002", "T-0003"]  # labels kept
    assert s.item("T-0001").fields["subject"] == "Edited subject"
    assert [i.item_id for i in s.items(limit=10)][-2:] == ["T-0006", "T-0007"]  # appended
    assert [h.kind for h in s.snapshots()] == ["refresh", "take"]
    with pytest.raises(ConnectorError):
        snapshot.apply(ctx, d)  # no longer pending

    # removed item comes back -> counted as new, restored in place with its labels
    write_rows(path, rows(8))
    d2 = snapshot.diff(ctx)
    assert d2.n_new == 2 and set(d2.new) == {"T-0002", "T-0003"} and d2.n_changed == 1
    snapshot.apply(ctx, d2)
    assert not s.item("T-0002").removed and s.count() == 8
    assert s.item("T-0002").ordinal == 2


def test_stale_diff_refused(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(3))
    snapshot.take(ctx)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(4))
    d1 = snapshot.diff(ctx)
    d2 = snapshot.diff(ctx)
    with pytest.raises(ConnectorError):
        snapshot.apply(ctx, d1)  # replaced by d2
    snapshot.apply(ctx, d2)
    assert ctx.store.count() == 4


def test_queue_change_is_a_change(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(3))
    snapshot.take(ctx)
    write_rows(ctx.project_dir / "tickets.jsonl", rows(3, {0: {"priority": "urgent"}}))
    d = snapshot.diff(ctx)
    assert d.changed == ["T-0000"]
    snapshot.apply(ctx, d)
    assert ctx.store.item("T-0000").queue == "urgent"


def test_labels_api_and_legacy_protocol(tmp_path: Path) -> None:
    store = ProjectStore.open(tmp_path)
    store.set_project_version(3)
    store.append(label("a"))  # LabelStore protocol: stamps the current project version
    store.append_label(label("b", "bo"), project_version=4)
    store.append_label(label("a", "bo"), project_version=4)
    assert [r.item_id for r in store.load()] == ["a", "b", "a"]
    assert [r.labeller for r in store.labels(item_id="a")] == ["ana", "bo"]
    assert [r.item_id for r in store.labels(labeller="bo")] == ["b", "a"]
    assert len(store.labels(offset=1, limit=1)) == 1
    assert [v for _, v in store.iter_labels(batch_size=2)] == [3, 4, 4]
    assert store.count_labels() == 3
    store.close()
    again = ProjectStore.open(tmp_path)
    assert len(again.load()) == 3  # durable


def test_session_runs_on_project_store(tmp_path: Path, make_session) -> None:
    """dev 3 can reuse state.Session with a ProjectStore as its LabelStore."""
    legacy = make_session()
    store = ProjectStore.open(tmp_path / "p")
    sess = Session(legacy.dataset, legacy.config, store)
    rec = sess.label("g0", "ana", "correct")
    assert store.load()[0].record_id == rec.record_id


def test_row_number_ids_stay_stable_when_source_grows(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path, {"card": {"fields": []}, "source": {"kind": "file", "path": "tickets.jsonl"}}
    )
    path = ctx.project_dir / "tickets.jsonl"
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(9999)))
    snapshot.take(ctx)
    assert ctx.store.items(limit=1)[0].item_id == "row-0000"
    with path.open("a") as fh:
        fh.write(json.dumps({"n": 9999}) + "\n")
    d = snapshot.diff(ctx)
    assert (d.n_new, d.n_removed, d.n_changed) == (1, 0, 0) and d.new == ["row-9999"]


def test_golden_answer_hidden_without_card_fields(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, {"card": {"fields": []}})
    write_rows(ctx.project_dir / "tickets.jsonl", rows(2))
    snapshot.take(ctx)
    it = ctx.store.item("T-0000")
    assert "answer" not in it.fields and it.golden_answer == "correct"
    assert "internal" in it.fields
