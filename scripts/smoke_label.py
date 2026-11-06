"""Offline smoke for `caliper label`: two labellers, export, kappa. No server, no browser.

Starts the app in-process (FastAPI TestClient) on the catalog_cleanup example
with double labelling on, has two fictional labellers label the whole golden set
through the HTTP API (including a skip and an undo), then:

* reads the agreement view (inter-annotator kappa, overall and by category);
* exports golden rows and labeled rollouts;
* feeds the rollouts to ``caliper_kappa.compute_kappa`` with no conversion,
  scoring production's own decision (auto_publish = 1) against the humans.

Run from the repo root:
    uv run python scripts/smoke_label.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from caliper_kappa import LabeledRow, LabelSet, compute_kappa
from caliper_label.app import create_app
from caliper_label.config import load_label_config
from caliper_label.data import load_dataset
from caliper_label.export import ExportFormat, export
from caliper_label.state import Session
from caliper_label.store import load_records, open_store
from caliper_sdk import EvalRow, GraderResult, Rollout
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "catalog_cleanup"


def legacy() -> int:
    config = load_label_config(EX / "label.yaml").model_copy(update={"double_label": 2})
    dataset = load_dataset(EX / "golden.jsonl", config)
    work = Path(tempfile.mkdtemp(prefix="caliper-label-smoke-"))
    store_path = work / "labels.jsonl"
    client = TestClient(create_app(Session(dataset, config, open_store(store_path))))

    def label(who: str, decide) -> int:
        n = 0
        while True:
            body = client.get(f"/api/next?labeller={who}").json()
            if body["done"]:
                return n
            item = dataset.by_id[body["item"]["id"]]
            r = client.post(
                "/api/label", json={"item_id": item.id, "labeller": who, "decision": decide(item)}
            )
            assert r.status_code == 200, r.text
            n += 1

    # ana judges with the synthetic ground truth; ben trusts high confidence.
    def ana(item):
        m = item.raw["metadata"]
        return "correct" if m["production_tax_code"] == m["true_tax_code"] else "wrong"

    def ben(item):
        return "correct" if item.raw["metadata"]["production_score"] >= 0.8 else "wrong"

    # a skip and an undo along the way
    first = client.get("/api/next?labeller=ana").json()["item"]["id"]
    client.post("/api/label", json={"item_id": first, "labeller": "ana", "skip": True})
    client.post("/api/undo", json={"labeller": "ana"})
    n_ana = label("ana", ana)
    n_ben = label("ben", ben)
    print(f"labelled: ana {n_ana}, ben {n_ben} (double_label=2)")

    agr = client.get("/api/agreement").json()
    o = agr["overall"]
    kappa = "undefined" if o["kappa"] is None else f"{o['kappa']:.3f}"
    print(
        f"inter-annotator ({agr['method']}): kappa {kappa} on {o['n_items']} items, "
        f"all agree {o['percent_all_agree']:.0%}"
    )
    for c in agr["by_category"]:
        k = "undefined" if c["kappa"] is None else f"{c['kappa']:.2f}"
        print(f"  {c['category']:<8} n={c['n_items']:<3} kappa={k}")

    records = load_records(store_path)
    golden_out, rollouts_out = work / "golden.labelled.jsonl", work / "rollouts.jsonl"
    n_golden, cons = export(dataset, config, records, golden_out, ExportFormat.GOLDEN)
    n_roll, _ = export(dataset, config, records, rollouts_out, ExportFormat.ROLLOUTS)
    print(
        f"export: {n_golden} golden rows, {n_roll} rollouts "
        f"(left out: {len(cons.conflicts)} conflicts, {len(cons.incomplete)} incomplete)"
    )

    rows = [EvalRow.model_validate_json(line) for line in rollouts_out.read_text().splitlines()]
    labels = LabelSet(
        name="consensus",
        rows=[
            LabeledRow(row_id=r.id, label_score=float(r.rollout.metadata["human_label"]))
            for r in rows
        ],
    )

    def production(rollout: Rollout) -> GraderResult:
        return GraderResult(score=1.0 if rollout.metadata["production_score"] >= 0.75 else 0.0)

    rep = compute_kappa(production, rows, labels)
    pk = "undefined" if rep.kappa_undefined else f"{rep.kappa:.3f}"
    print(f"production auto-publish vs human consensus: kappa {pk} on {rep.n_scored} rows")

    ok = (
        n_ana > 0
        and n_ben == n_ana
        and o["n_items"] == n_ana
        and n_golden == n_roll == len(cons.resolved) > 0
        and rep.n_scored == n_roll
        and any(r.undoes for r in records)
    )
    print("legacy: OK" if ok else "legacy: FAILED")
    print(f"(files in {work})", file=sys.stderr)
    return 0 if ok else 1


def project() -> int:
    """Project mode in team mode, in-process."""
    import csv
    import io
    import json
    import random

    from caliper_label import snapshot
    from caliper_label.connectors import open_connector
    from caliper_label.context import ProjectContext, _SnapshotArgs
    from caliper_label.project import ProjectConfig
    from caliper_label.project_store import ProjectStore

    work = Path(tempfile.mkdtemp(prefix="caliper-label-smoke-project-"))
    (work / "data").mkdir()
    rows = [json.loads(x) for x in (EX / "golden.jsonl").read_text().splitlines()]
    with (work / "data" / "listings.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "listing", "code", "true_code", "score", "stratum", "known"])
        for i, r in enumerate(rows):
            m = r["metadata"]
            known = (
                ("correct" if m["production_tax_code"] == m["true_tax_code"] else "wrong")
                if i % 20 == 5
                else ""
            )
            w.writerow(
                [
                    r["id"],
                    r["request"]["messages"][-1]["content"],
                    m["production_tax_code"],
                    m["true_tax_code"],
                    m["production_score"],
                    r["stratum"],
                    known,
                ]
            )
    cfg = ProjectConfig.model_validate(
        {
            "name": "Catalog cleanup (smoke)",
            "source": {"kind": "file", "path": "data/listings.csv", "id_column": "id"},
            "card": {
                "fields": [
                    {"column": "listing", "type": "long_text"},
                    {"column": "code", "type": "badge"},
                    {"column": "score", "type": "number", "decimals": 2},
                    {"column": "true_code", "hidden": True},
                ]
            },
            "labels": {
                "type": "single",
                "category_column": "code",
                "options": [
                    {"value": "correct", "label": "Code correct", "hotkey": "1", "approved": True},
                    {"value": "wrong", "label": "Wrong code", "hotkey": "2"},
                ],
            },
            "workflow": {
                "queues": {"by": "stratum"},
                "double_label": 2,
                "stage": "smoke",
                "golden": {"answer_column": "known", "rate": 0.1},
            },
        }
    )
    store = ProjectStore.open(work)
    con = open_connector(cfg.source, work)
    snapshot.take(_SnapshotArgs(work, cfg, store, con))
    ctx = ProjectContext(work, cfg, store, con, version=1)
    token = "smoke-fictional-token"
    c = TestClient(create_app(ctx=ctx, auth_token=token))
    assert c.get("/api/items").status_code == 401
    h = {"Authorization": f"Bearer {token}"}
    rnd = random.Random(7)

    def judge(who: str, item: dict) -> str:
        f = {x["label"]: x["value"] for x in item["fields"]}
        truth = "correct" if store.item(item["id"]).fields["true_code"] == f["code"] else "wrong"
        return (
            truth
            if who != "cy" or rnd.random() > 0.3
            else ("wrong" if truth == "correct" else "correct")
        )

    counts = {}
    for who, quota in (("ana", 40), ("cy", 40), ("ben", 80)):
        n = 0
        while n < quota:
            body = c.get(f"/api/next?labeller={who}&n=5", headers=h).json()
            if body["done"]:
                break
            item = body["item"]
            r = c.post(
                "/api/labels",
                headers=h,
                json={"item_id": item["id"], "labeller": who, "decision": judge(who, item)},
            )
            assert r.status_code == 200, r.text
            n += 1
        counts[who] = n
    dash = c.get("/api/dashboard", headers=h).json()
    o = dash["agreement"]["overall"]
    gold = {x["name"]: x["golden"] for x in dash["labellers"] if x["golden"]}
    print(
        f"project: labelled {counts}; kappa {o['kappa']:.3f} on {o['n_items']} items; "
        f"golden accuracy {{{', '.join(f'{k}: {v["accuracy"]:.0%}' for k, v in gold.items())}}}"
    )
    open_ = c.get("/api/adjudications", headers=h).json()
    for d in open_["items"]:
        c.post(
            "/api/adjudications",
            headers=h,
            json={
                "item_id": d["item_id"],
                "adjudicator": "lee",
                "decision": "wrong",
                "note": "smoke",
            },
        )
    text = c.get("/api/export/download?format=csv", headers=h).text
    table = list(csv.DictReader(io.StringIO(text)))
    prov = {"labeller", "labelled_at", "note", "source", "stage", "project_version"}
    roll = [
        EvalRow.model_validate_json(x)
        for x in c.get("/api/export/download?format=rollouts", headers=h).text.splitlines()
    ]
    labels = LabelSet(
        name="consensus",
        rows=[
            LabeledRow(row_id=r.id, label_score=float(r.rollout.metadata["human_label"]))
            for r in roll
        ],
    )

    def production(rollout: Rollout) -> GraderResult:
        score = next(
            float(x.split(": ")[1]) for x in rollout.prompt.splitlines() if x.startswith("score")
        )
        return GraderResult(score=1.0 if score >= 0.75 else 0.0)

    rep = compute_kappa(production, roll, labels)
    adjudicated = sum(1 for r in table if r["status"] == "adjudicated")
    print(
        f"project export: {len(table)} csv rows ({adjudicated} adjudicated), {len(roll)} rollouts; "
        f"production vs consensus kappa {rep.kappa:.3f}"
    )
    ok = (
        len(open_["items"]) > 0
        and adjudicated == len(open_["items"])
        and prov <= set(table[0])
        and all(r["project_version"] == "1" for r in table)
        and len(gold) >= 2
        and rep.n_scored == len(roll) > 0
    )
    print(f"(files in {work})", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    rc = legacy() or project()
    print("smoke_label: OK" if rc == 0 else "smoke_label: FAILED")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
