"""Prepare the dry-run server's store for the GIF recording.

Writes the labeled rollouts from examples/invoice_totals into a SQLite
store as `invoice_totals` (all rows) and `invoice_spot_check` (9 rows), so
the editor can dry-run against them via `?evalSet=...`. Also checks that the one-grader
editor snippets in this directory score every row exactly like the
graders in examples/invoice_totals/graders.py, so the GIF can't drift
from the example.

    uv run python demo/record/seed_store.py sqlite:///build/record/store.db
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from caliper_sdk import EvalRow, load_grader_from_source
from caliper_store import SqlStore

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = Path(os.environ.get("CALIPER_DEMO_EXAMPLE_DIR", ROOT / "examples" / "invoice_totals"))
HERE = Path(__file__).resolve().parent
# Small, ordered subset for the editor: approved answers first, reward hacks
# last, so the results panel's "highest scoring rows" surfaces the hacks.
SPOT_CHECK = [
    "lr-000", "lr-007", "lr-014", "lr-020", "lr-028", "lr-036", "lr-031", "lr-034", "lr-043",
]
SNIPPETS = {"naive_grader": HERE / "editor_naive.py", "fixed_grader": HERE / "editor_fixed.py"}


def main(url: str) -> int:
    rows = [
        EvalRow.model_validate_json(line)
        for line in (EXAMPLE / "labeled_rollouts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spec = importlib.util.spec_from_file_location("invoice_graders", EXAMPLE / "graders.py")
    assert spec is not None and spec.loader is not None
    graders = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(graders)

    for name, path in SNIPPETS.items():
        snippet = load_grader_from_source(path.read_text(encoding="utf-8")).grader
        reference = getattr(graders, name)
        for row in rows:
            a, b = snippet(row.rollout).score, reference(row.rollout).score
            if a != b:
                print(f"{path.name} disagrees with graders.{name} on {row.id}: {a} != {b}")
                return 1

    by_id = {r.id: r for r in rows}
    store = SqlStore(url)
    for name, subset in (
        ("invoice_totals", rows),
        ("invoice_spot_check", [by_id[i] for i in SPOT_CHECK]),
    ):
        v = store.create(name, subset, description="examples/invoice_totals labeled rollouts")
        print(f"seeded {name}@{v.version} ({v.n_rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
