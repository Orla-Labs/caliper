"""Offline end-to-end smoke for `caliper-compile` (trl, bedrock, openai-legacy).

For each target: compile an inline grader + small dataset via the CLI, check
the output is byte-identical across two runs, then EXECUTE the emitted
artifact in an isolated interpreter (`python -I -S`: no site-packages, so no
caliper / pydantic) with sample trainer inputs:

* trl     -> call reward_funcs.REWARD_FUNC the way GRPOTrainer does
* bedrock -> call lambda_function.lambda_handler with a documented event batch
* openai  -> exec the python grader source from rft_job.json, call grade()

No network, no API keys, nothing is trained.
Run from repo root: `uv run python scripts/smoke_compile_targets.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

GRADER_SOURCE = '''\
import re

from caliper_sdk import GraderResult, Rollout, grader

_TOTAL = re.compile(r"total:\\s*\\$?(\\d+(?:\\.\\d+)?)", re.IGNORECASE)


@grader
def total_matches(rollout: Rollout) -> GraderResult:
    m = _TOTAL.search(rollout.response)
    fmt = 1.0 if m else 0.0
    ok = 1.0 if m and rollout.gold and float(m.group(1)) == float(rollout.gold) else 0.0
    return GraderResult(score=0.2 * fmt + 0.8 * ok, components={"format": fmt, "correct": ok})


@grader
def always_half(rollout: Rollout) -> float:
    return 0.5
'''

ROWS = [
    {"id": "inv-1", "rollout": {"prompt": "2 x $10. Total?", "response": "", "gold": "20",
                                "metadata": {"currency": "USD"}}},
    {"id": "inv-2", "rollout": {"prompt": "3 x $7.50. Total?", "response": "", "gold": "22.5",
                                "metadata": {}}},
]

PRELUDE = """
import importlib.util, json, sys
for m in ("caliper_sdk", "caliper_rft", "pydantic"):
    try:
        __import__(m)
        raise SystemExit("isolation broken: " + m)
    except ImportError:
        pass
def load(path):
    spec = importlib.util.spec_from_file_location("emitted", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
OUT = sys.argv[1]
"""

RUNNERS = {
    "trl": """
rf = load(OUT + "/reward_funcs.py")
rows = [json.loads(l) for l in open(OUT + "/train.jsonl", encoding="utf-8")]
cols = {k: [r[k] for r in rows for _ in range(2)] for k in rows[0] if k != "prompt"}
out = rf.REWARD_FUNC(
    prompts=[r["prompt"] for r in rows for _ in range(2)],
    completions=[[{"role": "assistant", "content": c}]
                 for c in ("Total: $20", "total 20", "TOTAL: 22.50", "Total: 23")],
    completion_ids=[[0]] * 4, trainer_state=None, **cols)
print(rf.REWARD_FUNC.__name__, json.dumps(out))
""",
    "bedrock": """
lf = load(OUT + "/lambda_function.py")
rows = [json.loads(l) for l in open(OUT + "/train.jsonl", encoding="utf-8")]
event = [{
    "id": r["id"],
    "messages": r["messages"] + [{"role": "assistant", "content": resp}],
    "metadata": {"reference_answer": r["reference_answer"], "caliper": r["caliper"]},
} for r, resp in zip(rows, ("Total: $20", "Total: 23"))]
print(json.dumps(lf.lambda_handler(event, None), sort_keys=True))
""",
    "openai": """
job = json.load(open(OUT + "/rft_job.json", encoding="utf-8"))
ns = {}
exec(job["method"]["reinforcement"]["grader"]["source"], ns)
rows = [json.loads(l) for l in open(OUT + "/train.jsonl", encoding="utf-8")]
print(json.dumps([ns["grade"]({"output_text": t}, row)
                  for row, t in zip(rows, ("Total: $20", "nope"))]))
""",
}

EXPECTED = {
    "trl": 'total_matches [1.0, 0.0, 1.0, 0.2]',
    "bedrock": None,  # checked structurally below
    "openai": "[1.0, 0.0]",
}


def _compile(target: str, work: Path, out: Path, *, quiet: bool = False) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-m", "caliper_rft.cli", "--target", target,
         "--grader", f"{work / 'graders.py'}:total_matches",
         "--dataset", str(work / "rows.jsonl"), "--out", str(out)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"[smoke] compile {target} failed:\n{proc.stderr}")
    if proc.stderr and not quiet:
        print("  stderr: " + proc.stderr.strip().replace("\n", "\n  stderr: "))
    return proc.stdout


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "graders.py").write_text(GRADER_SOURCE, encoding="utf-8")
        (work / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ROWS),
                                         encoding="utf-8")
        for target, body in RUNNERS.items():
            print(f"[smoke] --target {target}")
            out_a, out_b = work / target / "a", work / target / "b"
            stdout = _compile(target, work, out_a)
            _compile(target, work, out_b, quiet=True)
            print("  " + stdout.strip().replace(str(work) + "/", "").replace("\n", "\n  "))
            files = sorted(p.name for p in out_a.iterdir())
            for name in files:
                if (out_a / name).read_bytes() != (out_b / name).read_bytes():
                    raise SystemExit(f"[smoke] FAIL: {target}/{name} is not deterministic")
            print(f"  deterministic: {', '.join(files)} byte-identical across runs")

            proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
                [sys.executable, "-I", "-S", "-c", PRELUDE + body, str(out_a)],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode != 0:
                raise SystemExit(f"[smoke] FAIL running emitted {target} artifact:\n{proc.stderr}")
            result = proc.stdout.strip()
            print(f"  emitted artifact (python -I -S, no caliper): {result}")
            if target == "bedrock":
                parsed = json.loads(result)
                scores = [r["aggregate_reward_score"] for r in parsed]
                assert [r["id"] for r in parsed] == ["inv-1", "inv-2"], parsed
                assert scores == [1.0, 0.2], scores
                assert {m["type"] for m in parsed[0]["metrics_list"]} == {"Reward", "Metric"}
            elif result != EXPECTED[target]:
                raise SystemExit(f"[smoke] FAIL: {target} expected {EXPECTED[target]!r}")
    print("[smoke] OK: trl, bedrock, openai(legacy) compiled and executed offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
