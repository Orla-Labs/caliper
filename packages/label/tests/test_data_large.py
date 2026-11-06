"""1M-row Parquet: snapshot, paginate and refresh with bounded memory (``-m slow``).

The workload runs in a fresh subprocess so its peak RSS (``ru_maxrss`` of the child) is not
polluted by whatever this pytest process did before. Numbers are printed (``-s`` to see them).
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

pytestmark = pytest.mark.slow

N = 1_000_000
RSS_LIMIT_MB = 500  # measured ~300 MB on macOS arm64 (see docs/label-connectors.md)

WORKLOAD = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from types import SimpleNamespace
    from caliper_label import snapshot
    from caliper_label.connectors import open_connector
    from caliper_label.project import ProjectConfig
    from caliper_label.project_store import ProjectStore

    proj = Path(sys.argv[1])
    cfg = ProjectConfig.model_validate({
        "source": {"kind": "file", "path": "big.parquet", "id_column": "item_id"},
        "card": {"fields": [{"column": "subject"}, {"column": "body", "type": "long_text"},
                            {"column": "score", "type": "number"}]},
        "workflow": {"queues": {"by": "priority"}},
    })
    conn = open_connector(cfg.source, proj)
    ctx = SimpleNamespace(project_dir=proj, config=cfg, store=ProjectStore.open(proj),
                          connector=conn)
    out = {}
    t = time.perf_counter(); out["count"] = conn.count(); out["count_s"] = time.perf_counter() - t
    t = time.perf_counter(); conn.preview(50); conn.schema()
    out["preview_s"] = time.perf_counter() - t
    t = time.perf_counter(); info = snapshot.take(ctx); out["take_s"] = time.perf_counter() - t
    out["n_items"] = info.n_items
    s = ctx.store
    t = time.perf_counter()
    first = s.items(0, 50)
    deep = s.items(offset=999_950, limit=50)
    q = s.items(offset=300_000, limit=50, queue="high")
    keyset = s.items(after=deep[0].ordinal, limit=50)
    out["page_s"] = time.perf_counter() - t
    out["pages"] = [first[0].item_id, deep[-1].item_id, q[0].queue, len(keyset)]
    t = time.perf_counter(); out["count_high"] = s.count("high")
    out["count_q_s"] = time.perf_counter() - t
    t = time.perf_counter(); d = snapshot.diff(ctx); out["diff_s"] = time.perf_counter() - t
    out["diff"] = [d.n_new, d.n_removed, d.n_changed, d.n_unchanged]
    t = time.perf_counter(); snapshot.apply(ctx, d); out["apply_s"] = time.perf_counter() - t
    print(json.dumps(out))
    """
)


def test_one_million_rows_snapshot_and_paginate(tmp_path: Path) -> None:
    proj = tmp_path / "big"
    proj.mkdir()
    duckdb.connect().execute(
        f"""COPY (SELECT 'it-' || lpad(i::VARCHAR, 7, '0') AS item_id,
                  'Fictional subject ' || i AS subject,
                  repeat('Fictional body text for row ' || i || '. ', 4) AS body,
                  ['low', 'medium', 'high'][1 + i % 3] AS priority,
                  (i % 1000) / 10.0 AS score,
                  'https://example.invalid/items/' || i AS url,
                  'unused column ' || i AS extra
           FROM range({N}) t(i)) TO '{proj / "big.parquet"}' (FORMAT parquet)"""
    )
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    proc = subprocess.run(
        [sys.executable, "-c", WORKLOAD, str(proj)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    assert peak >= before
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    out["peak_rss_mb"] = round(peak_mb, 1)
    out["db_mb"] = round((proj / ".caliper-label" / "project.db").stat().st_size / 2**20, 1)
    print(
        "\n1M-row results:",
        json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}),
    )
    assert out["count"] == N and out["n_items"] == N
    assert out["pages"] == ["it-0000000", "it-0999999", "high", 49]
    assert out["count_high"] == N // 3
    assert out["diff"] == [0, 0, 0, N]
    assert peak_mb < RSS_LIMIT_MB, f"peak RSS {peak_mb:.0f} MB"
    assert out["page_s"] < 2.0
