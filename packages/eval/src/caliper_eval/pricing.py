"""Cache-aware price table (USD per million tokens).

Anthropic first-party list prices were copied from the official pricing page:
https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-11-06).
``Price.cache_write`` is the 5-minute cache-write price; ``cache_read`` is the
"cache hits and refreshes" price.

Not covered on purpose:

* OpenAI-compatible endpoints: prices vary per vendor and were not verified,
  so none are hardcoded. Add them under ``[pricing."<model>"]`` in the config.
* Amazon Bedrock sets its own prices (regional endpoints carry a 10% premium).
  A Bedrock model ID (``anthropic.claude-...``) is priced at the first-party
  list price as an *approximation* and flagged with a warning, because pricing
  it at $0 would silently disable the budget guard.

An unknown model costs 0 and is reported by :func:`pricing_warnings` so the
report can show it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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
PRICES: dict[str, Price] = {
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
    warning: str | None = None


def lookup_price(
    model: str,
    overrides: Mapping[str, Price] | None = None,
    *,
    provider: ProviderKind | str | None = None,
) -> PriceLookup:
    """Find a price and say how trustworthy it is (``warning`` is None when exact)."""
    overrides = overrides or {}
    if model in overrides:
        return PriceLookup(overrides[model])
    if model in PRICES and str(provider) != ProviderKind.BEDROCK:
        return PriceLookup(PRICES[model])
    base = normalize_model_id(model)
    if base in overrides:
        return PriceLookup(overrides[base])
    if base in PRICES:
        if str(provider) == ProviderKind.BEDROCK or base != model:
            return PriceLookup(
                PRICES[base],
                f"{model}: priced at Anthropic first-party list price; Bedrock sets its own "
                "prices (regional endpoints +10%). Add [pricing] to be exact.",
            )
        return PriceLookup(PRICES[base])
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


def pricing_warnings(
    candidates: Iterable[Candidate], overrides: Mapping[str, Price] | None = None
) -> dict[str, str]:
    """candidate name -> warning, for candidates whose cost is unknown or approximate."""
    out: dict[str, str] = {}
    for c in candidates:
        found = lookup_price(c.model, overrides, provider=c.provider)
        if found.warning:
            out[c.name] = found.warning
    return out
