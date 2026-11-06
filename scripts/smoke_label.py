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


def main() -> int:
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
    print("smoke_label: OK" if ok else "smoke_label: FAILED")
    print(f"(files in {work})", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
