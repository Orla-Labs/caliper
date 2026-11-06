"""TRL target: library reward func, dataset export, emitted reward_funcs.py.

Never imports trl/torch/datasets; TRL's call convention is simulated per
https://huggingface.co/docs/trl/main/en/grpo_trainer.
"""

from __future__ import annotations

import ast
import json
import logging

import pytest
from caliper_rft import to_trl_reward_func
from caliper_rft.targets import GraderSource, load_dataset
from caliper_rft.targets.trl import export_trl, render_reward_funcs, trl_record

ATTRS = [
    "exact_total",
    "with_components",
    "lazy_import",
    "raises",
    "out_of_range",
    "returns_bool",
    "gold_is_none",
]

# A GRPO-style batch: conversational, prompts repeated per generation.
CONV_KWARGS = {
    "prompts": [[{"role": "user", "content": "Invoice: total?"}]] * 2
    + [[{"role": "user", "content": "ünïcödé ✓"}]] * 2,
    "completions": [
        [{"role": "assistant", "content": "Total: $20"}],
        [{"role": "assistant", "content": "total: 21"}],
        [{"role": "assistant", "content": "TOTAL: 22.50"}],
        [{"role": "assistant", "content": [{"type": "text", "text": "nothing"}]}],
    ],
    "completion_ids": [[1], [2], [3], [4]],
    "gold": ["20", "20", "22.5", None],
    "caliper_metadata": ['{"weight": 0.5}', '{"weight": 0.5}', "{}", '{"weight": 3}'],
    "id": ["a", "a", "b", "b"],
    "trainer_state": None,
}


def test_reward_func_standard_format(sdk_graders) -> None:
    f = to_trl_reward_func(sdk_graders.exact_total)
    assert f.__name__ == "exact_total"
    out = f(completions=["Total: 20", "total: 3"], prompts=["p", "p"], gold=["20", "20"])
    assert out == [1.0, 0.0]


def test_reward_func_conversational_format(sdk_graders) -> None:
    f = to_trl_reward_func(sdk_graders.exact_total)
    assert f(**CONV_KWARGS) == [1.0, 0.0, 1.0, 0.0]


def test_reward_func_name_uses_grader_name(sdk_graders) -> None:
    assert to_trl_reward_func(sdk_graders.with_components).__name__ == "total_with_parts"
    assert to_trl_reward_func(sdk_graders.exact_total, name="my reward!").__name__ == "my_reward_"


def test_reward_func_passes_prompt_gold_metadata(sdk_graders) -> None:
    seen = []

    from caliper_sdk import grader

    @grader
    def spy(rollout):
        seen.append(rollout)
        return 0.5

    f = to_trl_reward_func(spy)
    assert f(**CONV_KWARGS) == [0.5] * 4
    assert seen[0].prompt == "Invoice: total?"
    assert seen[0].response == "Total: $20"
    assert seen[0].gold == "20"
    assert seen[0].metadata == {"weight": 0.5}
    assert seen[3].gold is None and seen[3].response == "nothing"


def test_reward_func_accepts_dict_metadata_and_missing_columns(sdk_graders) -> None:
    f = to_trl_reward_func(sdk_graders.with_components)
    assert f(completions=["total: 1"], metadata=[{"weight": 0.0}]) == [0.5]
    assert f(completions=["x"]) == [0.5]  # no prompts/gold/metadata columns at all


def test_reward_func_failures_score_on_error_and_are_counted(sdk_graders, caplog) -> None:
    f = to_trl_reward_func(sdk_graders.raises)
    with caplog.at_level(logging.WARNING, logger="caliper.reward"):
        assert f(completions=["a", "b", "c"]) == [0.0, 0.0, 0.0]
    assert f.scorer.failures == 3
    assert len([r for r in caplog.records if "raised" in r.getMessage()]) == 1  # first only
    assert to_trl_reward_func(sdk_graders.out_of_range, on_error=None)(completions=["a"]) == [None]
    assert to_trl_reward_func(sdk_graders.returns_bool, on_error=0.25)(completions=["a"]) == [0.25]


def test_trl_record_shape(fixtures_dir) -> None:
    rows = load_dataset(fixtures_dir / "dataset.jsonl")
    rec = trl_record(rows[0])
    assert rec == {
        "id": "r1",
        "prompt": [{"role": "user", "content": rows[0].rollout.prompt}],
        "gold": "20",
        "caliper_metadata": '{"human_label": 1, "weight": 0.5}',
    }
    assert trl_record(rows[2])["gold"] is None


def test_export_trl_files(tmp_path, fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:exact_total")
    rows = load_dataset(fixtures_dir / "dataset.jsonl")
    assert export_trl(g, rows, tmp_path) == ["train.jsonl", "reward_funcs.py"]
    lines = (tmp_path / "train.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["r1", "r2", "r3"]
    src = (tmp_path / "reward_funcs.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))
    assert str(tmp_path) not in src and str(fixtures_dir) not in src
    assert "https://huggingface.co/docs/trl" in src


def test_render_is_deterministic(fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:exact_total")
    assert render_reward_funcs(g) == render_reward_funcs(g)


_PARITY_BODY = """
mod = load(ARGS["path"])
f = mod.REWARD_FUNC
assert mod.reward_funcs == [f]
print(json.dumps({"name": f.__name__, "rewards": f(**ARGS["kwargs"]),
                  "failures": f.scorer.failures}))
"""


@pytest.mark.parametrize("attr", ATTRS)
def test_emitted_reward_funcs_matches_library(attr, tmp_path, fixtures_dir, sdk_graders,
                                              run_isolated) -> None:
    """Same grader, same TRL batch: emitted file (no caliper) == library API (real SDK)."""
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:{attr}")
    export_trl(g, load_dataset(fixtures_dir / "dataset.jsonl"), tmp_path)
    lib = to_trl_reward_func(getattr(sdk_graders, attr))
    expected = lib(**CONV_KWARGS)
    got = run_isolated(_PARITY_BODY, {"path": str(tmp_path / "reward_funcs.py"),
                                      "kwargs": CONV_KWARGS})
    assert got["rewards"] == expected
    assert got["name"] == lib.__name__
    assert got["failures"] == lib.scorer.failures


def test_emitted_reward_funcs_on_exported_dataset(tmp_path, fixtures_dir, run_isolated) -> None:
    """Feed the emitted train.jsonl rows back as TRL would (columns as kwargs)."""
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:with_components")
    export_trl(g, load_dataset(fixtures_dir / "dataset.jsonl"), tmp_path)
    body = """
mod = load(ARGS["dir"] + "/reward_funcs.py")
rows = [json.loads(l) for l in open(ARGS["dir"] + "/train.jsonl", encoding="utf-8")]
kw = {k: [r[k] for r in rows] for k in rows[0] if k != "prompt"}
out = mod.REWARD_FUNC(prompts=[r["prompt"] for r in rows],
                      completions=["Total: 1", "nope", "total: 2"], **kw)
print(json.dumps(out))
"""
    assert run_isolated(body, {"dir": str(tmp_path)}) == [0.75, 0.5, 1.0]
