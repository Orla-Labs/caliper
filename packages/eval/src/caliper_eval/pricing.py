"""Where a candidate's price comes from (USD per million tokens, cache-aware).

Prices come from the provider whenever the provider publishes them:

1. ``[pricing."<model>"]`` in the config always wins (exact, your number).
2. Amazon Bedrock (``bedrock`` and ``bedrock_converse``): the live AWS Price
   List offer files (:mod:`caliper_eval.aws_pricing`), for the candidate's
   region and inference tier. No hardcoded Bedrock prices.
3. Anthropic API direct (``anthropic``): Anthropic publishes no pricing API
   (the Models API returns capabilities, not prices), so the only source is
   :data:`ANTHROPIC_LIST_PRICES`, copied from the pricing page and dated. Every
   use is reported as a warning so nobody mistakes it for a live price. A
   Bedrock candidate whose live lookup fails falls back to it too, flagged.
4. Anything else (``openai_compat``, ``fake``) has no price source: add
   ``[pricing]``. An unknown price costs $0 in the dollar column and is a
   warning; under ``[ci] max_cost_ratio`` it fails the gate, because $0 would
   otherwise look free.

``Price.cache_write`` is the 5-minute cache-write price; ``cache_read`` is the
"cache hits and refreshes" price.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from caliper_eval.aws_pricing import BedrockPrice, bedrock_price
from caliper_eval.types import Candidate, Price, ProviderKind, Usage

_SRC = "https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-11-06)"


def _p(inp: float, cache_write: float, cache_read: float, out: float, note: str = "") -> Price:
    return Price(
        input=inp,
        output=out,
        cache_read=cache_read,
        cache_write=cache_write,
        source=_SRC + (f"; {note}" if note else ""),
    )


# model id -> Price. Columns on the source page: base input, 5m cache write,
# (1h cache write, not modelled), cache hits & refreshes, output.
ANTHROPIC_LIST_PRICES: dict[str, Price] = {
    "claude-fable-5-1": _p(10.0, 12.50, 0.25, 50.0),
    "claude-mythos-5-1": _p(10.0, 12.50, 0.25, 50.0),
    "claude-fable-5": _p(10.0, 12.50, 1.00, 50.0),
    "claude-mythos-5": _p(10.0, 12.50, 1.00, 50.0),
    "claude-opus-5-5": _p(4.0, 5.00, 0.20, 20.0),
    "claude-opus-5": _p(5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-8": _p(5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-7": _p(5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-6": _p(5.0, 6.25, 0.50, 25.0),
    "claude-sonnet-5-5": _p(2.0, 2.50, 0.10, 10.0),
    "claude-sonnet-5": _p(2.0, 2.50, 0.20, 10.0),
    "claude-sonnet-4-6": _p(3.0, 3.75, 0.30, 15.0),
    "claude-haiku-5-5": _p(
        0.10, 0.125, 0.01, 0.50, "prompts <= 100K tokens; prompts over 100K are billed higher"
    ),
    "claude-haiku-4-5": _p(1.0, 1.25, 0.10, 5.0),
}

_BEDROCK_PREFIXES = ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.")
_BEDROCK_KINDS = (ProviderKind.BEDROCK, ProviderKind.BEDROCK_CONVERSE)

BedrockLookup = Callable[[str, str | None], BedrockPrice]
_bedrock_cache: dict[tuple[str, str | None], BedrockPrice] = {}


def _cached_bedrock_price(model: str, region: str | None) -> BedrockPrice:
    key = (model, region)
    if key not in _bedrock_cache:
        _bedrock_cache[key] = bedrock_price(model, region)
    return _bedrock_cache[key]


def normalize_model_id(model: str) -> str:
    """Strip Bedrock decorations: ``us.anthropic.claude-x`` -> ``claude-x``."""
    m = model
    for prefix in _BEDROCK_PREFIXES:
        if m.startswith(prefix):
            m = m[len(prefix) :]
            break
    if m.startswith("anthropic."):
        m = m[len("anthropic.") :]
    return m


@dataclass(frozen=True)
class PriceLookup:
    price: Price | None
    warning: str | None = None  # None when the price is exact and from a live/explicit source


def _table_warning(model: str) -> str:
    return (
        f"{model}: priced from Caliper's built-in Anthropic list-price table "
        "(Anthropic publishes no pricing API; checked 2026-11-06). Add [pricing] to override."
    )


def lookup_price(
    model: str,
    overrides: Mapping[str, Price] | None = None,
    *,
    provider: ProviderKind | str | None = None,
    region: str | None = None,
    bedrock: BedrockLookup | None = None,
) -> PriceLookup:
    """Find a price for ``model`` and say how far to trust it (see module docstring)."""
    overrides = overrides or {}
    base = normalize_model_id(model)
    for key in (model, base):
        if key in overrides:
            return PriceLookup(overrides[key])

    kind = ProviderKind(provider) if provider is not None else None
    if kind in _BEDROCK_KINDS:
        found = (bedrock or _cached_bedrock_price)(model, region)
        if found.price is not None:
            return PriceLookup(found.price, "; ".join(found.warnings) or None)
        if base in ANTHROPIC_LIST_PRICES:
            return PriceLookup(
                ANTHROPIC_LIST_PRICES[base],
                f"{found.error}. Fell back to Anthropic's first-party list price "
                "(Bedrock may differ). Add [pricing] to be exact.",
            )
        return PriceLookup(None, f"{found.error}; cost reported as $0. Add [pricing].")

    if kind in (None, ProviderKind.ANTHROPIC) and base in ANTHROPIC_LIST_PRICES:
        return PriceLookup(ANTHROPIC_LIST_PRICES[base], _table_warning(model))
    return PriceLookup(None, f"{model}: no price known; cost reported as $0. Add [pricing].")


def price_for(model: str, overrides: Mapping[str, Price] | None = None) -> Price | None:
    return lookup_price(model, overrides).price


def cost_usd(usage: Usage, price: Price | None) -> float:
    """Cost of one reply. Cache reads/writes are priced separately from input."""
    if price is None:
        return 0.0
    cache_read = price.input if price.cache_read is None else price.cache_read
    cache_write = price.input if price.cache_write is None else price.cache_write
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
    )
    return total / 1_000_000


def candidate_price(
    candidate: Candidate, overrides: Mapping[str, Price] | None = None
) -> PriceLookup:
    """Price lookup for a candidate's requested model, in its provider and region."""
    return lookup_price(
        candidate.model, overrides, provider=candidate.provider, region=candidate.region
    )


def pricing_warnings(
    candidates: Iterable[Candidate], overrides: Mapping[str, Price] | None = None
) -> dict[str, str]:
    """candidate name -> warning, for candidates whose cost is unknown or approximate."""
    out: dict[str, str] = {}
    for c in candidates:
        found = candidate_price(c, overrides)
        if found.warning:
            out[c.name] = found.warning
    return out


def unpriced_candidates(
    candidates: Iterable[Candidate], overrides: Mapping[str, Price] | None = None
) -> set[str]:
    """Names of candidates with no price at all (their dollars read $0)."""
    return {c.name for c in candidates if candidate_price(c, overrides).price is None}
