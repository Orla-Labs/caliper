"""Mutations for graders of classification-style outputs (verdict + rationale).

When a model classifies items ("is this a refund request?", "is this listing a
duplicate?"), its response is a **verdict** (one label from a fixed set) plus a
**rationale**. A grader for that output should reward a correct verdict that
is *justified*, not just any string that contains the right label. These
mutations produce the degenerate classifiers that a weak grader lets through:

    ConstantAnswer        the same label + boilerplate for every item
    ClassPriorAnswer      the majority class + "most items are X"
    SurfaceFeatureOnly    a keyword-match verdict with a keyword-only rationale
    VerdictRationaleContradiction
                          verdict unchanged, rationale now argues another label
    Hedging               a non-committal verdict naming every label

All five are ``expected_effect = LOWER``: each output is worse than a real,
justified classification. They are **opt-in**: they need the label set (and
for some, keywords or a majority class), so build them with
:func:`classification_mutations` and pass them to ``run_mutations`` yourself.
The eight built-in text mutations are unchanged.

Parsing the response is delegated to a format (:class:`JsonVerdictFormat` by
default, :class:`TaggedVerdictFormat` for ``Verdict: ... / Rationale: ...``
text). A response the format cannot parse is returned unchanged, so the
runner records it as a no-op skip rather than an error.

Everything is deterministic: no randomness, label order is the order given.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from caliper_sdk import EvalRow, Rollout

from caliper_mutate.types import Mutation, MutationExpectation

# --------------------------------------------------------------------------- formats


@runtime_checkable
class VerdictFormat(Protocol):
    """How a classification response is split into (verdict, rationale) and rebuilt."""

    def parse(self, response: str) -> tuple[str, str] | None: ...

    def render(self, response: str, verdict: str, rationale: str) -> str: ...


class JsonVerdictFormat:
    """Response is a JSON object, e.g. ``{"verdict": "refund", "rationale": "..."}``.

    Other keys are preserved (in order) when the response is rebuilt.
    """

    def __init__(self, verdict_key: str = "verdict", rationale_key: str = "rationale") -> None:
        self.verdict_key = verdict_key
        self.rationale_key = rationale_key

    def _load(self, response: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(response)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def parse(self, response: str) -> tuple[str, str] | None:
        obj = self._load(response)
        if obj is None or not isinstance(obj.get(self.verdict_key), str):
            return None
        rationale = obj.get(self.rationale_key, "")
        return obj[self.verdict_key], rationale if isinstance(rationale, str) else ""

    def render(self, response: str, verdict: str, rationale: str) -> str:
        obj = self._load(response) or {}
        obj[self.verdict_key] = verdict
        obj[self.rationale_key] = rationale
        return json.dumps(obj, ensure_ascii=False)


class TaggedVerdictFormat:
    """Plain text with ``Verdict: <label>`` and ``Rationale: <text>`` lines."""

    _VERDICT_RE = re.compile(r"^\s*verdict\s*:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
    _RATIONALE_RE = re.compile(
        r"^\s*rationale\s*:\s*(.*)\Z", re.IGNORECASE | re.MULTILINE | re.DOTALL
    )

    def parse(self, response: str) -> tuple[str, str] | None:
        v = self._VERDICT_RE.search(response)
        if v is None:
            return None
        r = self._RATIONALE_RE.search(response)
        return v.group(1), (r.group(1).strip() if r else "")

    def render(self, response: str, verdict: str, rationale: str) -> str:
        return f"Verdict: {verdict}\nRationale: {rationale}"


_DEFAULT_FORMAT = JsonVerdictFormat()


def _rewrite(rollout: Rollout, fmt: VerdictFormat, new: tuple[str, str] | None) -> Rollout:
    if new is None:
        return rollout.model_copy()
    response = fmt.render(rollout.response, new[0], new[1])
    return rollout.model_copy(update={"response": response})


# --------------------------------------------------------------------------- mutations


class ConstantAnswer:
    """Answer every item with the same label and a canned, item-free rationale."""

    name: str = "constant_answer"
    expected_effect: MutationExpectation = MutationExpectation.LOWER

    def __init__(self, label: str, *, fmt: VerdictFormat = _DEFAULT_FORMAT) -> None:
        self.label = label
        self.fmt = fmt
        self.description = (
            f"Replace the output with verdict {label!r} and a canned rationale that "
            "says nothing about the item (a constant classifier)."
        )

    def apply(self, rollout: Rollout) -> Rollout:
        if self.fmt.parse(rollout.response) is None:
            return rollout.model_copy()
        rationale = f"Classified as {self.label}."
        return _rewrite(rollout, self.fmt, (self.label, rationale))


class ClassPriorAnswer:
    """Answer every item with the majority class, justified by the base rate."""

    name: str = "class_prior_answer"
    expected_effect: MutationExpectation = MutationExpectation.LOWER

    def __init__(self, majority_label: str, *, fmt: VerdictFormat = _DEFAULT_FORMAT) -> None:
        self.majority_label = majority_label
        self.fmt = fmt
        self.description = (
            f"Replace the output with the majority class {majority_label!r} and a "
            "base-rate rationale (a class-prior classifier)."
        )

    @classmethod
    def from_eval_set(
        cls, eval_set: Iterable[EvalRow], *, fmt: VerdictFormat = _DEFAULT_FORMAT
    ) -> ClassPriorAnswer:
        """Majority of ``rollout.gold`` over the eval set (ties: alphabetically first)."""
        return cls(majority_class(eval_set), fmt=fmt)

    def apply(self, rollout: Rollout) -> Rollout:
        if self.fmt.parse(rollout.response) is None:
            return rollout.model_copy()
        rationale = f"Most items are {self.majority_label}, so this one most likely is too."
        return _rewrite(rollout, self.fmt, (self.majority_label, rationale))


class SurfaceFeatureOnly:
    """Decide by keyword match on the prompt; the rationale cites only the keyword."""

    name: str = "surface_feature_only"
    expected_effect: MutationExpectation = MutationExpectation.LOWER

    def __init__(
        self,
        keywords: Mapping[str, str],
        fallback_label: str,
        *,
        fmt: VerdictFormat = _DEFAULT_FORMAT,
    ) -> None:
        if not keywords:
            raise ValueError("SurfaceFeatureOnly needs at least one keyword -> label")
        self.keywords = dict(keywords)
        self.fallback_label = fallback_label
        self.fmt = fmt
        self.description = (
            "Replace the verdict with a keyword match on the prompt (first matching "
            f"keyword of {list(self.keywords)}, else {fallback_label!r}) and a rationale "
            "that only names the keyword."
        )

    def apply(self, rollout: Rollout) -> Rollout:
        if self.fmt.parse(rollout.response) is None:
            return rollout.model_copy()
        prompt = rollout.prompt.lower()
        for keyword, label in self.keywords.items():
            if keyword.lower() in prompt:
                return _rewrite(rollout, self.fmt, (label, f"The item mentions '{keyword}'."))
        return _rewrite(rollout, self.fmt, (self.fallback_label, "No keyword matched."))


class VerdictRationaleContradiction:
    """Keep the verdict; make the rationale conclude a different label."""

    name: str = "verdict_rationale_contradiction"
    expected_effect: MutationExpectation = MutationExpectation.LOWER

    def __init__(self, labels: Sequence[str], *, fmt: VerdictFormat = _DEFAULT_FORMAT) -> None:
        if len(labels) < 2:
            raise ValueError("VerdictRationaleContradiction needs at least 2 labels")
        self.labels = list(labels)
        self.fmt = fmt
        self.description = (
            "Keep the verdict but append a conclusion to the rationale that argues "
            "for a different label (the next one in the label list)."
        )

    def apply(self, rollout: Rollout) -> Rollout:
        parsed = self.fmt.parse(rollout.response)
        if parsed is None:
            return rollout.model_copy()
        verdict, rationale = parsed
        if verdict in self.labels:
            other = self.labels[(self.labels.index(verdict) + 1) % len(self.labels)]
        else:
            other = self.labels[0]
        conclusion = f"So this is not {verdict}; it is {other}."
        new_rationale = f"{rationale} {conclusion}".strip()
        return _rewrite(rollout, self.fmt, (verdict, new_rationale))


class Hedging:
    """Refuse to commit: the verdict names every label, the rationale hedges."""

    name: str = "hedging"
    expected_effect: MutationExpectation = MutationExpectation.LOWER

    def __init__(self, labels: Sequence[str], *, fmt: VerdictFormat = _DEFAULT_FORMAT) -> None:
        if len(labels) < 2:
            raise ValueError("Hedging needs at least 2 labels")
        self.labels = list(labels)
        self.fmt = fmt
        self.description = (
            "Replace the verdict with every label joined by ' or ' and prefix the "
            "rationale with a hedge."
        )

    def apply(self, rollout: Rollout) -> Rollout:
        parsed = self.fmt.parse(rollout.response)
        if parsed is None:
            return rollout.model_copy()
        hedge = " or ".join(self.labels)
        rationale = f"Hard to say; it could be {hedge}. {parsed[1]}".strip()
        return _rewrite(rollout, self.fmt, (hedge, rationale))


# --------------------------------------------------------------------------- helpers


def majority_class(eval_set: Iterable[EvalRow]) -> str:
    """Most common ``rollout.gold`` (ties broken alphabetically). Raises if none."""
    counts = Counter(r.rollout.gold for r in eval_set if r.rollout.gold is not None)
    if not counts:
        raise ValueError("no row has a gold label; pass majority_label explicitly")
    best = max(counts.values())
    return min(label for label, c in counts.items() if c == best)


def classification_mutations(
    labels: Sequence[str],
    *,
    keywords: Mapping[str, str] | None = None,
    majority_label: str | None = None,
    eval_set: Iterable[EvalRow] | None = None,
    constant_label: str | None = None,
    fmt: VerdictFormat = _DEFAULT_FORMAT,
) -> list[Mutation]:
    """The opt-in classification set, in a fixed order.

    * ``labels`` - the allowed verdicts, in a stable order.
    * ``keywords`` - ``{keyword: label}`` for :class:`SurfaceFeatureOnly`
      (checked in the given order; no match gives ``labels[-1]``). The
      mutation is left out when not given.
    * ``majority_label`` or ``eval_set`` - for :class:`ClassPriorAnswer`;
      with only ``eval_set``, the majority of ``rollout.gold`` is used
      (:func:`majority_class`). One of the two is required.
    * ``constant_label`` - for :class:`ConstantAnswer`; defaults to ``labels[0]``.
      Pick a label other than the majority class, or the two mutations coincide.
    """
    if len(labels) < 2:
        raise ValueError("a classification needs at least 2 labels")
    if majority_label is None:
        if eval_set is None:
            raise ValueError("pass majority_label or eval_set (to compute the class prior)")
        majority_label = majority_class(eval_set)
    muts: list[Mutation] = [
        ConstantAnswer(constant_label or labels[0], fmt=fmt),
        ClassPriorAnswer(majority_label, fmt=fmt),
    ]
    if keywords:
        fallback = labels[-1]
        muts.append(SurfaceFeatureOnly(keywords, fallback, fmt=fmt))
    muts += [
        VerdictRationaleContradiction(labels, fmt=fmt),
        Hedging(labels, fmt=fmt),
    ]
    return muts


CLASSIFICATION_MUTATION_TYPES: tuple[type, ...] = (
    ConstantAnswer,
    ClassPriorAnswer,
    SurfaceFeatureOnly,
    VerdictRationaleContradiction,
    Hedging,
)
"""The five classification mutation classes (opt-in; not in any default list)."""


__all__ = [
    "CLASSIFICATION_MUTATION_TYPES",
    "ClassPriorAnswer",
    "ConstantAnswer",
    "Hedging",
    "JsonVerdictFormat",
    "SurfaceFeatureOnly",
    "TaggedVerdictFormat",
    "VerdictFormat",
    "VerdictRationaleContradiction",
    "classification_mutations",
    "majority_class",
]
