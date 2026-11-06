"""Caliper RFT — typed grader specs, JSON compiler, and OpenAI passthrough.

See ``docs/03-graders-and-rewards.md`` for the format reference.

Public surface:
    Spec types          — StringCheckGrader, ScoreModelGrader, MultiGrader, ...
    Compiler            — compile_to_rft, compile_to_rft_json, validate_calculate_output
    Runner              — RFTRunner, build_payload, Job, JobStatus, RFTClient
    Real client         — OpenAIRFTClient (lazy openai import)
    Test double         — FakeRFTClient (in caliper_rft.testing)
    Targets             — compile_target, to_trl_reward_func (caliper_rft.targets; CLI
                          `caliper-compile`); OpenAI RFT is legacy/deprecated
"""

from caliper_rft.compile import (
    compile_to_rft,
    compile_to_rft_json,
    validate_calculate_output,
)
from caliper_rft.runner import (
    Job,
    JobStatus,
    RFTClient,
    RFTRunner,
    build_payload,
)
from caliper_rft.spec import (
    AnyGrader,
    LabelModelGrader,
    MultiGrader,
    PythonGrader,
    ScoreModelGrader,
    StringCheckGrader,
    TextSimilarityGrader,
)
from caliper_rft.targets import compile_target, to_trl_reward_func

__all__ = [
    "AnyGrader",
    "Job",
    "JobStatus",
    "LabelModelGrader",
    "MultiGrader",
    "PythonGrader",
    "RFTClient",
    "RFTRunner",
    "ScoreModelGrader",
    "StringCheckGrader",
    "TextSimilarityGrader",
    "build_payload",
    "compile_target",
    "compile_to_rft",
    "compile_to_rft_json",
    "to_trl_reward_func",
    "validate_calculate_output",
]
