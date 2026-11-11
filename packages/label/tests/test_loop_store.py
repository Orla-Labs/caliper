"""The labelling loop on dev 1's real ProjectStore + connector + snapshot (dev 3)."""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest
from caliper_label import snapshot
from caliper_label.app import create_app
from caliper_label.connectors import open_connector
from caliper_label.context import ProjectContext, _SnapshotArgs
from caliper_label.project import ProjectConfig
from caliper_label.project_store import ProjectStore
from fastapi.testclient import TestClient

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)


def project(tmp_path: Path, n: int = 10) -> ProjectContext:
    (tmp_path / "data" / "img").mkdir(parents=True)
    with (tmp_path / "data" / "tickets.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticket_id", "subject", "priority", "team", "known", "photo"])
        for i in range(n):
            (tmp_path / "data" / "img" / f"p{i}.png").write_bytes(PNG_1PX)
            w.writerow(
                [
                    f"t{i}",
                    f"Fictional ticket {i}",
                    "high" if i % 2 else "low",
                    "billing",
                    "yes" if i == 3 else "",
                    f"data/img/p{i}.png",
                ]
            )
    cfg = ProjectConfig.model_validate(
        {
            "name": "Store project (fictional)",
            "source": {"kind": "file", "path": "data/tickets.csv", "id_column": "ticket_id"},
            "card": {
                "fields": [
                    {"column": "subject"},
                    {"column": "team", "type": "badge"},
                    {"column": "photo", "type": "image"},
                ]
            },
            "labels": {"type": "yes_no_unsure", "category_column": "team"},
            "workflow": {
                "queues": {"by": "priority"},
                "golden": {"answer_column": "known", "rate": 0.0},
                "double_label": 2,
            },
        }
    )
    store = ProjectStore.open(tmp_path)
    con = open_connector(cfg.source, tmp_path)
    snapshot.take(_SnapshotArgs(tmp_path, cfg, store, con))
    store.set_project_version(2)
    return ProjectContext(tmp_path, cfg, store, con, version=2)


def test_loop_on_project_store(tmp_path: Path) -> None:
    ctx = project(tmp_path)
    c = TestClient(create_app(ctx=ctx))
    cfg = c.get("/api/label-config").json()
    assert cfg["queues"] == ["low", "high"] and cfg["n_items"] == 10 and cfg["golden"]
    q = c.get("/api/queues?labeller=ana").json()["queues"]
    assert [(x["name"], x["total"]) for x in q] == [("low", 5), ("high", 4)]  # t3 is golden
    nxt = c.get("/api/next?labeller=ana&n=3").json()
    assert [i["id"] for i in nxt["items"]] == ["t0", "t1", "t2"]
    img = next(f for f in nxt["item"]["fields"] if f["type"] == "image")
    assert img["src"] == "/api/media/t0/2"
    assert c.get(img["src"]).content == PNG_1PX  # dev 1's media route
    assert "known" not in str(nxt)
    r = c.post("/api/labels", json={"item_id": "t0", "labeller": "ana", "decision": "yes"})
    assert r.status_code == 200 and r.json()["record"]["project_version"] == 2
    c.post("/api/labels", json={"item_id": "t0", "labeller": "ben", "decision": "no"})
    # persisted in project.db with the version
    rows = ctx.store.label_rows()
    assert [(rec.labeller, v) for _, rec, v in rows] == [("ana", 2), ("ben", 2)]
    # a fresh context (server restart) resumes from the store
    again = ProjectContext(tmp_path, ctx.config, ProjectStore.open(tmp_path), None, version=2)
    assert again.session.state("t0").labels.keys() == {"ana", "ben"}
    d = c.get("/api/dashboard").json()
    assert d["totals"]["disagreements"] == 1
    c.post("/api/adjudications", json={"item_id": "t0", "adjudicator": "lee", "decision": "yes"})
    out = c.get("/api/export/download?format=csv").text
    assert "t0,low,billing,yes,Yes,True,1,adjudicated" in out


def test_reload_stamps_new_version_on_store(tmp_path: Path) -> None:
    ctx = project(tmp_path)
    new = ctx.config.model_copy(deep=True)
    ctx.reload(new)
    assert ctx.version == 3
    s = ctx.session
    s.label("t1", "ana", "no")
    assert ctx.store.label_rows()[-1][2] == 3


@pytest.mark.slow
def test_serving_stays_fast_on_a_million_items(tmp_path: Path) -> None:
    import duckdb

    (tmp_path / "data").mkdir()
    duckdb.sql(
        "COPY (SELECT 'r' || i AS id, 'fictional ' || i AS body, "
        "CASE WHEN i % 4 = 0 THEN 'high' ELSE 'low' END AS pri FROM range(1000000) t(i)) "
        f"TO '{tmp_path}/data/big.parquet' (FORMAT parquet)"
    )
    cfg = ProjectConfig.model_validate(
        {
            "name": "big",
            "source": {"kind": "file", "path": "data/big.parquet", "id_column": "id"},
            "card": {"fields": [{"column": "body"}]},
            "labels": {"type": "yes_no_unsure"},
            "workflow": {"queues": {"by": "pri"}, "double_label": 2},
        }
    )
    store = ProjectStore.open(tmp_path)
    con = open_connector(cfg.source, tmp_path)
    snapshot.take(_SnapshotArgs(tmp_path, cfg, store, con))
    s = ProjectContext(tmp_path, cfg, store, con).session
    for _ in range(200):
        it = s.next_item("ana")
        s.label(it.id, "ana", "yes")
    worst = 0.0
    for who, q in [("ana", None), ("ben", None), ("ana", "high"), ("cy", "low")]:
        t = time.perf_counter()
        assert len(s.next_items(who, q, 6)) == 6
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.1, f"next_items took {worst * 1000:.0f} ms"
    s.queues("ana")
    t = time.perf_counter()
    s.queues("ana")
    assert time.perf_counter() - t < 0.1


def test_cli_opens_and_exports_a_project_folder(monkeypatch, tmp_path: Path, capsys) -> None:
    import uvicorn
    from caliper_eval.cli import main
    from caliper_label.project import save_project

    ctx = project(tmp_path)  # writes the data + snapshot; now save the config file
    data = ctx.config.model_dump(mode="json")
    data["rollout"] = {"prompt": "subject", "response": "team"}
    save_project(tmp_path, ProjectConfig.model_validate(data))
    ctx.store.close()
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    assert main(["label", str(tmp_path), "--no-browser", "--port", "8789"]) == 0
    opened = started["app"].state.ctx
    assert opened is not None and opened.config.name == "Store project (fictional)"
    assert "10 items, 2 queue(s)" in capsys.readouterr().out
    s = opened.session
    s.label("t0", "ana", "yes")
    s.label("t0", "ben", "yes")
    opened.store.close()
    out = tmp_path / "out" / "labels.csv"
    assert main(["label", "export", str(tmp_path), "--format", "csv", "--out", str(out)]) == 0
    assert "wrote 1 csv row(s)" in capsys.readouterr().out
    assert (
        out.read_text().splitlines()[1].startswith("t0,low,billing,yes,Yes,True,1,agreed,2,ana;ben")
    )
    roll = tmp_path / "out" / "r.jsonl"
    assert main(["label", "export", str(tmp_path), "--format", "rollouts", "--out", str(roll)]) == 0
    import json

    row = json.loads(roll.read_text())
    assert row["rollout"]["prompt"] == "Fictional ticket 0"  # ProjectConfig.rollout paths
    assert row["rollout"]["response"] == "billing"
    capsys.readouterr()
    assert (
        main(["label", "export", str(tmp_path / "nope"), "--format", "csv", "--out", "x.csv"]) == 2
    )
    assert "fix: create one with the setup wizard" in capsys.readouterr().err
