"""Caliper kappa — agreement statistics.

Two questions:
    * does a grader agree with human labels?   -> compute_kappa (Cohen's)
    * do human labellers agree with each other? -> inter_rater_kappa
      (Cohen's for 2 raters, Fleiss' for 3+; overall and by category)

Public surface:
    compute_kappa     — grader vs labels
    KappaReport       — result type
    KappaWeighting    — unweighted | linear | quadratic
    LabelSet          — named collection of labeled rows
    LabeledRow        — (row_id, label_score)
    RowKappa          — per-row contribution
    RowKappaFailure   — per-row exclusion record
    DEFAULT_BIN_EDGES — pass/fail default ([0.0, 0.5, 1.0])
    bucketize         — score -> bucket index
    inter_rater_kappa — rater vs rater agreement (InterRaterReport,
                        AgreementStats, PairAgreement, RaterLabel)
"""

from ._bucketize import DEFAULT_BIN_EDGES, bucketize
from ._inter_rater import (
    UNCATEGORIZED,
    AgreementStats,
    InterRaterReport,
    PairAgreement,
    RaterLabel,
    inter_rater_kappa,
)
from ._kappa import compute_kappa
from ._types import (
    KappaReport,
    KappaWeighting,
    LabeledRow,
    LabelSet,
    RowKappa,
    RowKappaFailure,
)

__all__ = [
    "DEFAULT_BIN_EDGES",
    "UNCATEGORIZED",
    "AgreementStats",
    "InterRaterReport",
    "KappaReport",
    "KappaWeighting",
    "LabelSet",
    "LabeledRow",
    "PairAgreement",
    "RaterLabel",
    "RowKappa",
    "RowKappaFailure",
    "bucketize",
    "compute_kappa",
    "inter_rater_kappa",
]
