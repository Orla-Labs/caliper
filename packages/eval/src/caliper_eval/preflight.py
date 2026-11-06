"""Preflight: one minimal request per candidate, with its exact shape.

Fails fast, before any money is spent on the golden set, when a provider
rejects the shape (thinking mode, structured-output mode, schema) or answers
with a different model than requested.
"""

from __future__ import annotations

from collections.abc import Iterable

from caliper_eval.config import EvalConfig
from caliper_eval.loader import load_golden
from caliper_eval.providers import get_provider
from caliper_eval.providers.base import PreflightResult, error_text
from caliper_eval.types import Candidate

__all__ = ["PreflightError", "PreflightResult", "format_failures", "run_preflight"]


class PreflightError(RuntimeError):
    def __init__(self, failures: list[PreflightResult]) -> None:
        self.failures = failures
        super().__init__(format_failures(failures))


def run_preflight(
    candidates: Iterable[Candidate], config: EvalConfig | None = None
) -> list[PreflightResult]:
    """One result per candidate, in order. Never raises for provider problems."""
    base_dir = config.config_dir if config is not None else None
    pricing = config.pricing if config is not None else None
    sample_request = None
    if config is not None:
        try:
            rows = load_golden(config.golden_path)
            sample_request = rows[0].request if rows else None
        except (OSError, ValueError):
            sample_request = None
    results: list[PreflightResult] = []
    for candidate in candidates:
        try:
            provider = get_provider(candidate, base_dir=base_dir, pricing=pricing)
            results.append(provider.preflight(candidate, sample_request=sample_request))
        except Exception as exc:  # e.g. SDK extra not installed
            results.append(
                PreflightResult(candidate=candidate.name, ok=False, reason=error_text(exc))
            )
    return results


def format_failures(results: Iterable[PreflightResult]) -> str:
    lines = [f"preflight failed for {r.candidate}: {r.reason}" for r in results if not r.ok]
    return "\n".join(lines)


def require_preflight(candidates: Iterable[Candidate], config: EvalConfig | None = None) -> None:
    """Run preflight and raise PreflightError if any candidate fails."""
    failures = [r for r in run_preflight(candidates, config) if not r.ok]
    if failures:
        raise PreflightError(failures)
