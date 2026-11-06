"""Prepare the dry-run server's store for the GIF recording.

Writes an `invoice_totals` eval set (the labeled rollouts from
examples/invoice_totals) into a SQLite store, so the editor can dry-run
against it via `?evalSet=invoice_totals`. Also checks that the one-grader
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

    store = SqlStore(url)
    version = store.create(
        "invoice_totals",
        rows,
        description="examples/invoice_totals labeled rollouts (GIF recording)",
    )
    print(f"seeded invoice_totals@{version.version} ({version.n_rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
