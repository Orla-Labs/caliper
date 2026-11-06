"""Legacy OpenAI RFT target: rft_job.json with an inline python grader."""

from __future__ import annotations

import json

from caliper_rft.targets import GraderSource, load_dataset
from caliper_rft.targets.openai_legacy import (
    MODEL_PLACEHOLDER,
    TRAINING_FILE_PLACEHOLDER,
    build_rft_job,
    export_openai,
)


def test_rft_job_payload_shape(fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:exact_total")
    job = build_rft_job(g)
    assert job["model"] == MODEL_PLACEHOLDER
    assert job["training_file"] == TRAINING_FILE_PLACEHOLDER
    assert job["method"]["type"] == "reinforcement"
    grader = job["method"]["reinforcement"]["grader"]
    assert set(grader) == {"type", "source"} and grader["type"] == "python"
    assert "def grade(sample, item):" in grader["source"]
    assert len(grader["source"].encode()) < 256 * 1024


def test_export_openai_files(tmp_path, fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:exact_total")
    rows = load_dataset(fixtures_dir / "dataset.jsonl")
    assert export_openai(g, rows, tmp_path) == ["rft_job.json", "train.jsonl"]
    first = json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])
    assert first == {
        "messages": [{"role": "user", "content": rows[0].rollout.prompt}],
        "gold": "20",
        "caliper_metadata": '{"human_label": 1, "weight": 0.5}',
    }


def test_python_grader_runs_without_caliper(tmp_path, fixtures_dir, run_isolated) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:with_components")
    export_openai(g, load_dataset(fixtures_dir / "dataset.jsonl"), tmp_path)
    body = """
job = json.load(open(ARGS["dir"] + "/rft_job.json"))
ns = {}
exec(job["method"]["reinforcement"]["grader"]["source"], ns)
rows = [json.loads(l) for l in open(ARGS["dir"] + "/train.jsonl", encoding="utf-8")]
print(json.dumps([ns["grade"]({"output_text": t}, r)
                  for r, t in zip(rows, ["Total: 1", "x", "total: 2"])] +
                 [ns["grade"]({}, rows[0])]))
"""
    # weights 0.5 / 2->1 / default 1; last call: missing output_text -> "".
    assert run_isolated(body, {"dir": str(tmp_path)}) == [0.75, 0.5, 1.0, 0.25]
