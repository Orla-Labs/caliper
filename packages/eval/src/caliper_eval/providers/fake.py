"""Deterministic offline provider driven by a user script.

The candidate's ``fake_script`` (``"file.py:respond"``, relative to the config
directory) is called once per reply::

    def respond(request: dict, *, model: str, row_id: str, replica: int,
                sample: int) -> str | dict: ...

``request`` is the golden request overlaid with the candidate's settings:
``model``, ``thinking`` ("on"/"off"), ``thinking_budget_tokens``,
``output_mode``, ``output_schema`` and ``params`` keys merged in. A dict return
may hold ``text``, ``parsed``, ``served_model``, ``error`` and ``usage``
(``{"input_tokens": ..., "output_tokens": ..., "cache_read_tokens": ...,
"cache_write_tokens": ...}``). Exceptions become ``Reply.error``.

If the script module defines ``supports(candidate: dict) -> str | None``,
preflight calls it first; a string is the rejection reason, so examples can
show "preflight caught an unsupported shape" without network access.

Usage and latency are deterministic: tokens ~ characters / 4, latency derived
from a sha256 digest of (model, row, replica, sample) - never ``hash()``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

from caliper_eval.loader import load_callable, load_module, split_spec
from caliper_eval.providers.base import (
    PREFLIGHT_ROW_ID,
    PreflightResult,
    Provider,
    error_text,
    minimal_request,
    preflight_from_reply,
)
from caliper_eval.types import (
    Candidate,
    OutputMode,
    ProviderKind,
    Reply,
    ServedBy,
    Usage,
    canonical_json,
)


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _latency_ms(model: str, row_id: str, replica: int, sample: int) -> float:
    digest = hashlib.sha256(f"{model}|{row_id}|{replica}|{sample}".encode()).hexdigest()
    return float(50 + int(digest[:8], 16) % 950)


def fake_request(request: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    """The overlaid request the fake script receives (a copy; input untouched)."""
    out = copy.deepcopy(request)
    out["model"] = candidate.model
    out["thinking"] = str(candidate.thinking)
    if candidate.thinking_budget_tokens is not None:
        out["thinking_budget_tokens"] = candidate.thinking_budget_tokens
    out["output_mode"] = str(candidate.output_mode)
    if candidate.output_schema is not None:
        out["output_schema"] = copy.deepcopy(candidate.output_schema)
    out.update(copy.deepcopy(candidate.params))
    return out


class FakeProvider(Provider):
    kind = ProviderKind.FAKE

    def _respond_fn(self, candidate: Candidate):
        if not candidate.fake_script:
            raise ValueError(f"candidate {candidate.name!r}: provider 'fake' needs fake_script")
        return load_callable(candidate.fake_script, self.base_dir)

    def send(
        self,
        request: dict[str, Any],
        candidate: Candidate,
        *,
        row_id: str,
        replica: int,
        sample: int,
    ) -> Reply:
        sent = fake_request(request, candidate)
        latency = _latency_ms(candidate.model, row_id, replica, sample)
        input_tokens = _tokens(canonical_json(sent))
        try:
            raw = self._respond_fn(candidate)(
                sent, model=candidate.model, row_id=row_id, replica=replica, sample=sample
            )
        except Exception as exc:
            return Reply(
                error=error_text(exc),
                served_by=self._served(candidate, candidate.model),
                usage=Usage(input_tokens=input_tokens),
                latency_ms=latency,
            )
        data: dict[str, Any] = {"text": raw} if isinstance(raw, str) else dict(raw or {})
        text = data.get("text")
        parsed = data.get("parsed")
        error = data.get("error")
        if text is None and parsed is not None:
            text = canonical_json(parsed)
        if parsed is None and text is not None and candidate.output_mode != OutputMode.TEXT:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                error = error or f"structured output is not valid JSON: {exc}"
        usage_override = data.get("usage") or {}
        usage = Usage(
            input_tokens=usage_override.get("input_tokens", input_tokens),
            output_tokens=usage_override.get("output_tokens", _tokens(text or "")),
            cache_read_tokens=usage_override.get("cache_read_tokens", 0),
            cache_write_tokens=usage_override.get("cache_write_tokens", 0),
        )
        served_model = data.get("served_model") or candidate.model
        return Reply(
            text=text,
            parsed=parsed,
            served_by=self._served(candidate, served_model),
            usage=usage,
            latency_ms=latency,
            cost_usd=self.cost(usage, candidate, served_model),
            error=error,
        )

    @staticmethod
    def _served(candidate: Candidate, model: str) -> ServedBy:
        return ServedBy(
            provider=ProviderKind.FAKE,
            model=model,
            endpoint="fake",
            matches_request=model == candidate.model,
        )

    def preflight(
        self, candidate: Candidate, *, sample_request: dict[str, Any] | None = None
    ) -> PreflightResult:
        try:
            target, _ = split_spec(candidate.fake_script or "")
            module = (
                load_module(target, self.base_dir)
                if target.endswith(".py") or "/" in target
                else None
            )
        except Exception as exc:
            return PreflightResult(candidate=candidate.name, ok=False, reason=error_text(exc))
        supports = getattr(module, "supports", None) if module is not None else None
        if callable(supports):
            reason = supports(candidate.model_dump(mode="json"))
            if reason:
                return PreflightResult(
                    candidate=candidate.name,
                    ok=False,
                    reason=f"fake rejected the request shape (model={candidate.model}, "
                    f"thinking={candidate.thinking}, output_mode={candidate.output_mode}): "
                    f"{reason}",
                )
        request = sample_request if sample_request is not None else minimal_request(candidate)
        reply = self.send(request, candidate, row_id=PREFLIGHT_ROW_ID, replica=0, sample=0)
        return preflight_from_reply(candidate, reply)
