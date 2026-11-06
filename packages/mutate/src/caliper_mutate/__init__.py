"""Caliper mutation-testing for graders.

Public surface (filled in by the build-out):
    Mutation, MutationExpectation        — Protocol + enum (types.py)
    MutationOutcome, MutationFailure,
    MutationSummary, MutationReport      — report shapes (report.py)
    Built-in mutations (mutations.py)    — DropFormatGate, WrongAnswer, ...
    run_mutations, format_mutation_report (runner.py)
    Classification-grader mutations (classification.py, opt-in):
        classification_mutations(labels, ...) -> [ConstantAnswer,
        ClassPriorAnswer, SurfaceFeatureOnly, VerdictRationaleContradiction,
        Hedging]; JsonVerdictFormat / TaggedVerdictFormat parse the output.
"""

from caliper_mutate.classification import (
    CLASSIFICATION_MUTATION_TYPES,
    ClassPriorAnswer,
    ConstantAnswer,
    Hedging,
    JsonVerdictFormat,
    SurfaceFeatureOnly,
    TaggedVerdictFormat,
    VerdictFormat,
    VerdictRationaleContradiction,
    classification_mutations,
    majority_class,
)
from caliper_mutate.format import format_mutation_report
from caliper_mutate.mutations import (
    AppendGarbage,
    CasePerturb,
    DropFormatGate,
    HardcodeGold,
    PrependSycophancy,
    TruncateResponse,
    WhitespacePerturb,
    WrongAnswer,
)
from caliper_mutate.report import (
    MutationFailure,
    MutationOutcome,
    MutationReport,
    MutationSummary,
)
from caliper_mutate.runner import run_mutations
from caliper_mutate.types import Mutation, MutationExpectation

__all__ = [
    "CLASSIFICATION_MUTATION_TYPES",
    "AppendGarbage",
    "CasePerturb",
    "ClassPriorAnswer",
    "ConstantAnswer",
    "DropFormatGate",
    "HardcodeGold",
    "Hedging",
    "JsonVerdictFormat",
    "Mutation",
    "MutationExpectation",
    "MutationFailure",
    "MutationOutcome",
    "MutationReport",
    "MutationSummary",
    "PrependSycophancy",
    "SurfaceFeatureOnly",
    "TaggedVerdictFormat",
    "TruncateResponse",
    "VerdictFormat",
    "VerdictRationaleContradiction",
    "WhitespacePerturb",
    "WrongAnswer",
    "classification_mutations",
    "format_mutation_report",
    "majority_class",
    "run_mutations",
]
