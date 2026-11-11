"""Provider interface shared by fake, Anthropic (direct + Bedrock), Bedrock Converse and
OpenAI-compatible.

Rules every provider follows:

* ``send`` never raises for provider/transport problems: they become
  ``Reply.error`` so one bad row never kills a run.
* ``send`` never mutates the request it is given.
* Request shapes the user asked for are sent as-is. If a provider rejects a
  shape (thinking mode, forced tool use, structured output), that surfaces as
  an error - in ``preflight`` first - and is never silently downgraded.
* ``ServedBy`` is read from the response, never from the request.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from caliper_eval.pricing import cost_usd, lookup_price
from caliper_eval.types import Candidate, Price, ProviderKind, Reply, ServedBy, Usage

# Name of the tool used for OutputMode.TOOL (forced single tool call).
OUTPUT_TOOL_NAME = "caliper_output"

PREFLIGHT_ROW_ID = "__preflight__"


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    ok: bool
    reason: str | None = None
    served_by: ServedBy | None = None


def minimal_request(candidate: Candidate) -> dict[str, Any]:
    """Smallest request with the candidate's exact shape (used by preflight).

    ``max_tokens`` leaves room for an explicit thinking budget (which must be
    smaller than ``max_tokens``) and, in structured modes, for thinking plus a
    schema-shaped answer. Truncation does not fail preflight anyway: a response
    means the shape was accepted.
    """
    from caliper_eval.types import OutputMode

    room = 64 if candidate.output_mode == OutputMode.TEXT else 1024
    max_tokens = room + (candidate.thinking_budget_tokens or 0)
    return {
        "system": None,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": max_tokens,
    }


def models_match(requested: str, served: str | None) -> bool:
    """True when the response names the requested model (Bedrock prefixes ignored)."""
    if not served:
        return False
    from caliper_eval.pricing import normalize_model_id

    return served == requested or normalize_model_id(served) == normalize_model_id(requested)


def deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``extra`` into a copy of ``base``; nested dicts merge instead of replace."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Provider(ABC):
    kind: ProviderKind

    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        pricing: Mapping[str, Price] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.pricing = dict(pricing or {})

    @abstractmethod
    def send(
        self,
        request: dict[str, Any],
        candidate: Candidate,
        *,
        row_id: str,
        replica: int,
        sample: int,
    ) -> Reply: ...

    def preflight(
        self, candidate: Candidate, *, sample_request: dict[str, Any] | None = None
    ) -> PreflightResult:
        """Send one minimal request with the candidate's exact shape.

        Real providers always use :func:`minimal_request` (cheap); the fake
        provider uses ``sample_request`` (a golden row) when given, so user
        scripts see a realistic request.
        """
        reply = self.send(
            minimal_request(candidate), candidate, row_id=PREFLIGHT_ROW_ID, replica=0, sample=0
        )
        return preflight_from_response(candidate, reply)

    # helpers ---------------------------------------------------------------

    def cost(self, usage: Usage, candidate: Candidate, served: str | None = None) -> float:
        """Dollars for one reply. Priced as the requested model (whose ID carries the
        Bedrock tier) unless the response names a different model."""
        model = candidate.model if not served or models_match(candidate.model, served) else served
        found = lookup_price(model, self.pricing, provider=self.kind, region=candidate.region)
        return cost_usd(usage, found.price)


def preflight_from_response(candidate: Candidate, reply: Reply) -> PreflightResult:
    """Real providers: a response that names the requested model means the shape
    was accepted, even if the tiny preflight answer was truncated or did not
    parse (noted in ``reason``). No response, or another model, fails."""
    if reply.served_by is None:
        return preflight_from_reply(candidate, reply)
    if not reply.served_by.matches_request:
        return preflight_from_reply(candidate, reply.model_copy(update={"error": None}))
    note = f"shape accepted; preflight answer note: {reply.error}" if reply.error else None
    return PreflightResult(
        candidate=candidate.name, ok=True, reason=note, served_by=reply.served_by
    )


def preflight_from_reply(candidate: Candidate, reply: Reply) -> PreflightResult:
    if reply.error is not None:
        return PreflightResult(
            candidate=candidate.name,
            ok=False,
            reason=f"{candidate.provider} rejected the request shape "
            f"(model={candidate.model}, thinking={candidate.thinking}, "
            f"output_mode={candidate.output_mode}): {reply.error}",
            served_by=reply.served_by,
        )
    if reply.served_by is None or not reply.served_by.matches_request:
        served = reply.served_by.model if reply.served_by else "<unknown>"
        return PreflightResult(
            candidate=candidate.name,
            ok=False,
            reason=f"served model {served!r} != requested {candidate.model!r} "
            "(fallback or substitution; refusing to evaluate the wrong model)",
            served_by=reply.served_by,
        )
    return PreflightResult(candidate=candidate.name, ok=True, served_by=reply.served_by)


def error_text(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    prefix = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
    return f"{prefix}: {exc}"
