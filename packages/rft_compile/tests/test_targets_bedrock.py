"""Bedrock RFT target: dataset rows and the emitted Lambda reward function.

Event/response shapes per
https://docs.aws.amazon.com/bedrock/latest/userguide/reward-functions.html
"""

from __future__ import annotations

import ast
import json

import pytest
from caliper_rft.targets import GraderSource, load_dataset
from caliper_rft.targets._runtime import Scorer, bedrock_score_batch
from caliper_rft.targets.bedrock import bedrock_record, export_bedrock


def _scorer(sdk_graders, attr="with_components"):
    g = getattr(sdk_graders, attr)
    return Scorer(g, g.name, 0.0)


def test_bedrock_record_shape(fixtures_dir) -> None:
    rows = load_dataset(fixtures_dir / "dataset.jsonl")
    assert bedrock_record(rows[0]) == {
        "id": "r1",
        "messages": [{"role": "user", "content": rows[0].rollout.prompt}],
        "reference_answer": "20",
        "caliper": {"gold": "20", "metadata": {"human_label": 1, "weight": 0.5}},
    }
    rec = bedrock_record(rows[2])
    assert rec["reference_answer"] == "" and rec["caliper"]["gold"] is None


# Documented event: a list; reference_answer + extra fields under "metadata".
DOC_EVENT = [
    {
        "id": "123",
        "messages": [
            {"role": "user", "content": "Invoice: total?"},
            {"role": "assistant", "content": "Total: 20"},
        ],
        "metadata": {
            "reference_answer": "20",
            "caliper": {"gold": "20", "metadata": {"weight": 0.5}},
        },
    }
]


def test_documented_event_and_response_shape(sdk_graders) -> None:
    out = bedrock_score_batch(_scorer(sdk_graders), DOC_EVENT)
    assert out == [
        {
            "id": "123",
            "aggregate_reward_score": 0.75,
            "metrics_list": [
                {"name": "total_with_parts", "value": 0.75, "type": "Reward"},
                {"name": "total_with_parts.has_total", "value": 1.0, "type": "Metric"},
                {"name": "total_with_parts.weight", "value": 0.5, "type": "Metric"},
            ],
        }
    ]


@pytest.mark.parametrize(
    "event",
    [
        {"trajectories": DOC_EVENT},  # AWS blog sample handler shape
        DOC_EVENT[0],  # single item
        [  # caliper field at top level
            {**DOC_EVENT[0], "metadata": {}, "caliper": DOC_EVENT[0]["metadata"]["caliper"]}
        ],
    ],
)
def test_alternative_event_wrappers(event, sdk_graders) -> None:
    out = bedrock_score_batch(_scorer(sdk_graders), event)
    assert [r["aggregate_reward_score"] for r in out] == [0.75]


def test_non_caliper_rows_fall_back_to_reference_answer(sdk_graders) -> None:
    seen = []

    from caliper_sdk import grader

    @grader
    def spy(rollout):
        seen.append(rollout)
        return 1.0

    event = [
        {"id": "a", "messages": [{"role": "system", "content": "S"},
                                 {"role": "user", "content": "U"},
                                 {"role": "assistant", "content": "R"}],
         "reference_answer": {"solution": "x = 4"}, "metadata": {"my_key": "k"}},
        {"id": "b", "messages": [{"role": "user", "content": "U"}],
         "metadata": {"reference_answer": "plain", "my_key": "k2"}},
    ]
    out = bedrock_score_batch(Scorer(spy, "spy", 0.0), event)
    assert [r["id"] for r in out] == ["a", "b"]
    assert seen[0].prompt == "S\n\nU" and seen[0].response == "R"
    assert seen[0].gold == '{"solution": "x = 4"}' and seen[0].metadata == {"my_key": "k"}
    assert seen[1].response == "" and seen[1].gold == "plain"
    assert seen[1].metadata == {"my_key": "k2"}


def test_grader_failure_scores_zero(sdk_graders) -> None:
    s = _scorer(sdk_graders, "raises")
    out = bedrock_score_batch(s, DOC_EVENT)
    assert out[0]["aggregate_reward_score"] == 0.0 and s.failures == 1


def test_export_bedrock_files(tmp_path, fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:exact_total")
    assert export_bedrock(g, load_dataset(fixtures_dir / "dataset.jsonl"), tmp_path) == [
        "train.jsonl",
        "lambda_function.py",
    ]
    src = (tmp_path / "lambda_function.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))
    assert "def lambda_handler(event, context):" in src
    assert "docs.aws.amazon.com/bedrock" in src


_LAMBDA_BODY = """
lf = load(ARGS["dir"] + "/lambda_function.py")
rows = [json.loads(l) for l in open(ARGS["dir"] + "/train.jsonl", encoding="utf-8")]
event = [{"id": r["id"],
          "messages": r["messages"] + [{"role": "assistant", "content": c}],
          "metadata": {"reference_answer": r["reference_answer"], "caliper": r["caliper"]}}
         for r, c in zip(rows, ARGS["responses"])]
print(json.dumps(lf.lambda_handler(event, None)))
"""


@pytest.mark.parametrize("attr", ["with_components", "exact_total", "raises", "gold_is_none"])
def test_emitted_lambda_matches_library(attr, tmp_path, fixtures_dir, sdk_graders,
                                        run_isolated) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:{attr}")
    rows = load_dataset(fixtures_dir / "dataset.jsonl")
    export_bedrock(g, rows, tmp_path)
    responses = ["Total: $20", "total: 22.5", "no total"]
    got = run_isolated(_LAMBDA_BODY, {"dir": str(tmp_path), "responses": responses})

    records = [json.loads(line) for line in (tmp_path / "train.jsonl").read_text().splitlines()]
    event = [
        {"id": r["id"], "messages": [*r["messages"], {"role": "assistant", "content": c}],
         "metadata": {"reference_answer": r["reference_answer"], "caliper": r["caliper"]}}
        for r, c in zip(records, responses, strict=True)
    ]
    assert got == bedrock_score_batch(_scorer(sdk_graders, attr), event)
    assert [r["id"] for r in got] == ["r1", "r2", "r3"]
