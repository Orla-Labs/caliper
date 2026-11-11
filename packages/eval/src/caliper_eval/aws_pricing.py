"""Live Amazon Bedrock prices from the AWS Price List bulk files.

AWS publishes every Bedrock on-demand price as a public JSON "offer file"
(no credentials needed), one per service and region::

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/<region>/index.json

Two offers hold model inference prices (checked 2026-11-11):

* ``AmazonBedrock``: Amazon's own models and most third-party open models
  (Nova, Llama, Mistral, ...), priced per ``1K tokens``.
* ``AmazonBedrockFoundationModels``: Marketplace-billed models, including
  Claude ("Claude Haiku 5.5 (Amazon Bedrock Edition)"), priced per
  ``1M tokens``.

Both are read and every price is normalized to USD per million tokens from the
row's own ``unit`` field.

Rows name a model by display name (``"Nova 2.0 Lite"``, ``"Claude Haiku
5.5"``), not by model ID. The display name comes from Bedrock's
``GetFoundationModel`` (``"Nova 2 Lite"``), and the two are compared after one
normalization: a ``.0`` minor version is dropped (``2.0`` -> ``2``). Anything
else that does not match exactly is "no price", never a nearest match: pricing
Sonnet as Haiku would be worse than no price.

Tier, from the model ID prefix:

* ``global.`` -> the ``global`` rows (global cross-region inference);
* ``us.`` / ``eu.`` / ... or a bare ID -> the regional standard rows, which
  carry AWS's +10% regional premium. A bare ID is labelled "assumed", since the
  endpoint decides; the regional price is the higher one, the safe side for a
  cost ceiling.

Rows for batch, flex, priority, provisioned throughput, custom models,
training, 1-hour cache writes and long-context (large-prompt) rates are left
out. A model that has long-context rates gets a warning, because replies above
its prompt-size threshold cost more than reported.

The region is the candidate's ``region``, else ``AWS_REGION`` /
``AWS_DEFAULT_REGION``. Offer files and resolved model names are cached on disk
(``$CALIPER_CACHE_DIR`` or ``~/.cache/caliper``) for 24 hours. With
``CALIPER_PRICING_OFFLINE=1`` nothing goes to the network: only the cache is
read.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from caliper_eval.types import Price

OFFERS = ("AmazonBedrock", "AmazonBedrockFoundationModels")
OFFER_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{offer}/current/{region}/index.json"
)
CACHE_TTL_S = 24 * 3600

_GEO_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.")
_EXCLUDE = (
    "batch",
    "flex",
    "priority",
    "provisioned",
    "custom",
    "customization",
    "training",
    "storage",
    "grounding",
    "_1h",
    "-1h",
)
_UNITS = {"1k tokens": 1000.0, "1m tokens": 1.0}

# (offer, region) -> parsed offer file
OfferLoader = Callable[[str, str], dict[str, Any]]
# bedrock model id (no geo prefix) -> display name, or None when unknown
NameResolver = Callable[[str, str], str | None]


@dataclass(frozen=True)
class BedrockPrice:
    price: Price | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def split_model_id(model: str) -> tuple[str, str]:
    """``global.amazon.nova-2-lite-v1:0`` -> ``("global", "amazon.nova-2-lite-v1:0")``.

    Scope is ``global``, ``regional`` (geo inference profile) or ``bare``.
    """
    if model.startswith("global."):
        return "global", model[len("global.") :]
    for prefix in _GEO_PREFIXES:
        if model.startswith(prefix):
            return "regional", model[len(prefix) :]
    return "bare", model


def normalize_name(name: str) -> str:
    """``Nova 2.0 Lite`` and ``Nova 2 Lite`` -> ``nova 2 lite``. Nothing fuzzier."""
    return re.sub(r"\b(\d+)\.0\b", r"\1", name).strip().lower()


def _cache_dir() -> Path:
    root = os.environ.get("CALIPER_CACHE_DIR")
    return Path(root) if root else Path.home() / ".cache" / "caliper"


def default_offer_loader(offer: str, region: str) -> dict[str, Any]:
    """Offer file for ``offer`` in ``region``: disk cache first, then the network.

    Raises ``OSError`` / ``ValueError`` when neither works.
    """
    path = _cache_dir() / "aws-pricing" / offer / f"{region}.json"
    offline = _offline()
    fresh = path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_S
    if path.exists() and (fresh or offline):
        return json.loads(path.read_text(encoding="utf-8"))
    if offline:
        raise OSError(f"CALIPER_PRICING_OFFLINE=1 and no cached {offer} prices for {region}")
    import httpx

    url = OFFER_URL.format(offer=offer, region=region)
    try:
        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if path.exists():  # stale beats nothing; the price source says which file
            return json.loads(path.read_text(encoding="utf-8"))
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data


def _offline() -> bool:
    return os.environ.get("CALIPER_PRICING_OFFLINE") == "1"


def default_name_resolver(model_id: str, region: str) -> str | None:
    """Display name from Bedrock ``GetFoundationModel`` (needs AWS credentials), cached."""
    path = _cache_dir() / "aws-pricing" / f"model-names-{region}.json"
    try:
        names = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        names = {}
    if model_id in names:
        return names[model_id]
    if _offline():
        return None
    try:
        import boto3

        client = boto3.client("bedrock", region_name=region)
        name = client.get_foundation_model(modelIdentifier=model_id)["modelDetails"]["modelName"]
    except Exception:
        return None
    names[model_id] = name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(names, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return name


@dataclass
class _Row:
    kind: str  # input | output | cache_read | cache_write
    scope: str  # global | regional
    per_million: float
    long_context: bool


def _classify(usagetype: str) -> tuple[str | None, str, bool]:
    u = usagetype.lower()
    long_ctx = "long_ctx" in u or "long-ctx" in u
    if "cache-read" in u or "cache_read" in u:
        kind: str | None = "cache_read"
    elif "cache-write" in u or "cache_write" in u:
        kind = "cache_write"
    elif "input-token" in u or "input_token" in u:
        kind = "input"
    elif "output-token" in u or "output_token" in u:
        kind = "output"
    else:
        kind = None
    scope = "global" if "global" in u else "regional"
    return kind, scope, long_ctx


def _rows_for(data: dict[str, Any], display_name: str) -> Iterable[_Row]:
    want = normalize_name(display_name)
    on_demand = (data.get("terms") or {}).get("OnDemand") or {}
    for sku, product in (data.get("products") or {}).items():
        attrs = product.get("attributes") or {}
        if normalize_name(attrs.get("model") or "") != want:
            continue
        usagetype = attrs.get("usagetype") or ""
        kind, scope, long_ctx = _classify(usagetype)
        if kind is None:
            continue
        lowered = usagetype.lower()
        if any(x in lowered for x in _EXCLUDE):
            continue
        for term in (on_demand.get(sku) or {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                factor = _UNITS.get((dim.get("unit") or "").strip().lower())
                usd = (dim.get("pricePerUnit") or {}).get("USD")
                if factor is None or usd is None:
                    continue
                yield _Row(kind, scope, float(usd) * factor, long_ctx)


def _publication(data: dict[str, Any]) -> str:
    return str(data.get("publicationDate") or "unknown date")


def _known_names(datas: Iterable[dict[str, Any]]) -> list[str]:
    names = set()
    for data in datas:
        for product in (data.get("products") or {}).values():
            name = (product.get("attributes") or {}).get("model")
            if name:
                names.add(name)
    return sorted(names)


def bedrock_price(
    model: str,
    region: str | None,
    *,
    load_offer: OfferLoader = default_offer_loader,
    resolve_name: NameResolver = default_name_resolver,
) -> BedrockPrice:
    """On-demand price of a Bedrock ``model`` ID in ``region``, from AWS's offer files."""
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return BedrockPrice(
            None, error=f"{model}: no AWS region (set the candidate's region or AWS_REGION)"
        )
    scope, base_id = split_model_id(model)
    name = resolve_name(base_id, region)
    if not name:
        return BedrockPrice(
            None,
            error=f"{model}: Bedrock did not return a model name for {base_id} in {region} "
            "(needs AWS credentials with bedrock:GetFoundationModel)",
        )

    loaded: list[tuple[str, dict[str, Any]]] = []
    failures: list[str] = []
    for offer in OFFERS:
        try:
            loaded.append((offer, load_offer(offer, region)))
        except Exception as exc:
            failures.append(f"{offer}: {type(exc).__name__}: {exc}")
    if not loaded:
        return BedrockPrice(
            None, error=f"{model}: AWS price list unavailable ({'; '.join(failures)})"
        )

    want_scope = "global" if scope == "global" else "regional"
    found: dict[str, set[float]] = {}
    has_long_ctx = False
    sources: list[str] = []
    for offer, data in loaded:
        rows = list(_rows_for(data, name))
        if not rows:
            continue
        sources.append(f"{OFFER_URL.format(offer=offer, region=region)} ({_publication(data)})")
        for row in rows:
            if row.long_context:
                has_long_ctx = True
                continue
            if row.scope == want_scope:
                found.setdefault(row.kind, set()).add(row.per_million)

    if not found.get("input") or not found.get("output"):
        names = _known_names(d for _, d in loaded)
        words = set(normalize_name(name).split())
        near = [n for n in names if words & set(normalize_name(n).split())][:6]
        return BedrockPrice(
            None,
            error=f"{model}: no {want_scope} on-demand input/output price for model name "
            f"{name!r} in the AWS price list for {region}"
            + (f" (similar names there: {', '.join(near)})" if near else ""),
        )
    conflicts = sorted(k for k, v in found.items() if len(v) > 1)
    if conflicts:
        return BedrockPrice(
            None,
            error=f"{model}: the AWS price list has more than one {want_scope} price for "
            f"{name!r} ({', '.join(conflicts)}); refusing to guess",
        )

    def one(kind: str) -> float | None:
        values = found.get(kind)
        return next(iter(values)) if values else None

    tier = {"global": "global", "regional": "regional", "bare": "regional (assumed)"}[scope]
    price = Price(
        input=one("input") or 0.0,
        output=one("output") or 0.0,
        cache_read=one("cache_read"),
        cache_write=one("cache_write"),
        source=f"AWS price list, {name}, {region} {tier}: " + "; ".join(sources),
    )
    warnings: list[str] = []
    if scope == "bare":
        warnings.append(
            f"{model}: bare model ID priced at the regional rate (+10% over global); "
            "use a global./us. inference profile ID to be exact"
        )
    if has_long_ctx:
        warnings.append(
            f"{model}: {name} has higher long-context rates for large prompts; "
            "replies above that prompt size cost more than reported"
        )
    return BedrockPrice(price, warnings=warnings)
