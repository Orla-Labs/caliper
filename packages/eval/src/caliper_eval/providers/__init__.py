"""Model providers. ``get_provider(candidate)`` returns the right implementation.

Real providers import their SDKs lazily, so the core package and its tests work
without ``anthropic`` installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from caliper_eval.providers.base import PreflightResult, Provider
from caliper_eval.types import Candidate, Price, ProviderKind

__all__ = ["PreflightResult", "Provider", "get_provider"]


def get_provider(
    candidate: Candidate,
    *,
    base_dir: str | Path | None = None,
    pricing: Mapping[str, Price] | None = None,
) -> Provider:
    """Provider for ``candidate``. ``base_dir`` resolves relative ``fake_script``
    paths (the config directory); ``pricing`` holds config price overrides."""
    kind = ProviderKind(candidate.provider)
    if kind == ProviderKind.FAKE:
        from caliper_eval.providers.fake import FakeProvider

        return FakeProvider(base_dir=base_dir, pricing=pricing)
    if kind in (ProviderKind.ANTHROPIC, ProviderKind.BEDROCK):
        from caliper_eval.providers.anthropic import AnthropicProvider

        return AnthropicProvider(kind=kind, base_dir=base_dir, pricing=pricing)
    if kind == ProviderKind.BEDROCK_CONVERSE:
        from caliper_eval.providers.bedrock_converse import BedrockConverseProvider

        return BedrockConverseProvider(base_dir=base_dir, pricing=pricing)
    if kind == ProviderKind.OPENAI_COMPAT:
        from caliper_eval.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(base_dir=base_dir, pricing=pricing)
    raise ValueError(f"unknown provider {candidate.provider!r}")
