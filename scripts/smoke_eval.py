"""Offline end-to-end smoke for `caliper eval` on examples/catalog_cleanup.

No network, no API keys (fake provider). Checks:

1. `caliper eval --ci` runs all candidates, writes results/report/summary, and
   the gate fails for exactly the regressing candidate (exit 1).
2. report.json has the expected structure (noise floor, transitions,
   calibration with a recommendation, who answered).
3. Resume: truncate results.jsonl to half, re-run without --no-resume. Only the
   missing executions run, and stdout and report.json are identical.
4. Preflight rejects the json_schema + thinking candidate (exit 3) before
   writing any results.
5. `caliper sample` keeps every rare-stratum row.

Run from anywhere:  uv run python scripts/smoke_eval.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples" / "catalog_cleanup"


def caliper(*args: str) -> subprocess.CompletedProcess[str]:
    code = "import sys; from caliper_eval.cli import main; sys.exit(main())"
    return subprocess.run(  # noqa: S603 - fixed interpreter + our own args
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="caliper-smoke-") as tmp:
        out = Path(tmp) / "eval"
        args = [
            "eval", "--config", str(EX / "caliper.toml"), "--out", str(out), "--ci",
            "--summary-md", str(out / "summary.md"),
        ]  # fmt: skip

        # 1. full run
        first = caliper(*args, "--no-resume")
        check(first.returncode == 1, f"gate fails with exit 1 (got {first.returncode})")
        for name in ("results.jsonl", "report.json", "summary.md"):
            check((out / name).is_file(), f"wrote {name}")
        n_lines = len((out / "results.jsonl").read_text().splitlines())
        # baseline x2 replicas + small + small-thinking, 80 rows each
        check(n_lines == 4 * 80, f"320 executions recorded (got {n_lines})")

        # 2. report structure
        report = json.loads((out / "report.json").read_text())
        check(report["reference"] == "baseline", "reference is baseline")
        check(report["n_golden"] == 80, "80 golden rows")
        nf = report["noise_floor"]
        check(nf["n_replicas"] == 2 and nf["n_rows"] == 80, "noise floor from 2 replays x 80 rows")
        cands = {c["name"]: c for c in report["candidates"]}
        check(set(cands) == {"baseline", "small", "small-thinking"}, "three candidates reported")
        for name, c in cands.items():
            check(c["served_mismatches"] == 0, f"{name}: every reply served by the requested model")
            check(c["calibration"] is not None, f"{name}: calibration present")
            check(c["dollars"] > 0, f"{name}: cost recorded")
        check(cands["small"]["transitions"] is not None, "small: transitions vs reference")
        summary = (out / "summary.md").read_text()
        check(summary.startswith("## Caliper eval: FAIL"), "summary.md headline says FAIL")
        check("`small`: cost-weighted regression" in summary, "summary names the failing candidate")
        check(
            "`small-thinking`:" not in summary.split("**Gate failures**")[1],
            "small-thinking passes",
        )

        # 3. resume
        results = out / "results.jsonl"
        lines = results.read_text().splitlines(keepends=True)
        results.write_text("".join(lines[: len(lines) // 2]))
        report_before = (out / "report.json").read_text()
        summary_before = (out / "summary.md").read_text()
        second = caliper(*args)
        check(second.returncode == 1, "resumed run reaches the same verdict")
        check(second.stdout == first.stdout, "resumed stdout is byte-identical")
        check((out / "report.json").read_text() == report_before, "resumed report.json identical")
        check((out / "summary.md").read_text() == summary_before, "resumed summary.md identical")
        n_after = len(results.read_text().splitlines())
        check(n_after == n_lines, f"resume ran only the missing half (lines {n_after})")

        # 4. preflight rejection
        pre_out = Path(tmp) / "preflight"
        pre = caliper("eval", "--config", str(EX / "caliper.preflight.toml"), "--out", str(pre_out))
        check(pre.returncode == 3, f"preflight rejection exits 3 (got {pre.returncode})")
        check("small-thinking-schema" in pre.stderr, "preflight names the rejected candidate")
        check(
            not (pre_out / "results.jsonl").exists() or not (pre_out / "results.jsonl").read_text(),
            "nothing executed after a failed preflight",
        )

        # 5. sampler keeps the rare stratum whole
        sampled = Path(tmp) / "sampled.jsonl"
        smp = caliper(
            "sample", "--in", str(EX / "logs.jsonl"), "--out", str(sampled),
            "--stratum-field", "outcome", "--keep", "needs_review,fallback", "--negatives", "40",
            "--id-field", "id", "--reference-field", "outcome",
        )  # fmt: skip
        check(smp.returncode == 0, "caliper sample exits 0")
        logs = [json.loads(x) for x in (EX / "logs.jsonl").read_text().splitlines()]
        rows = [json.loads(x) for x in sampled.read_text().splitlines()]
        rare_ids = {r["id"] for r in logs if r["outcome"] != "auto_publish"}
        check(rare_ids <= {r["id"] for r in rows}, f"all {len(rare_ids)} review/fallback rows kept")
        check(len(rows) == len(rare_ids) + 40, f"plus 40 sampled negatives ({len(rows)} rows)")

    print("smoke_eval: all checks passed")


if __name__ == "__main__":
    main()
