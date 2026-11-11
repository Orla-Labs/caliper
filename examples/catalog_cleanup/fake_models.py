"""Deterministic fake models for the catalog-cleanup example.

Everything here is synthetic. The "models" read the supplier listing in the
request and answer from a small keyword knowledge table, with per-sample
jitter drawn from stable sha256 digests (never Python's ``hash()``), so every
run is byte-identical and needs no network or keys.

Behaviour, by design:

* ``fake-large`` knows every product family. Clear listings get high
  confidence; listings that mention two families (bundles, kids/adult
  lookalikes) get mid-range confidence and sometimes a different code, which
  is the baseline's natural replay noise. A few "lookalike" listings fool it
  confidently (that is what calibration against human approvals is for).
* ``fake-small`` (cheaper) does not know the rare families (loose batteries,
  home health devices, gift cards). It guesses a nearby common code with low,
  scattered confidence, so rare-class rows regress to review. It also emits
  truncated JSON now and then.
* ``fake-small`` with thinking on reasons its way to the rare code on most of
  those rows, at the price of extra output (thinking) tokens.
* ``supports()`` rejects ``fake-small`` with thinking on *and* json_schema
  output: the preflight fails fast before any money is spent.

Self-contained on purpose: Caliper loads it by path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# ----------------------------------------------------------------- knobs (tune here)

MODELS = ("fake-large", "fake-small")

CLEAR_CONF = {"fake-large": (0.93, 0.08), "fake-small": (0.91, 0.10)}  # (centre, spread)
MIXED_BASE = (0.62, 0.96)  # mixed listing: per-listing base confidence range (difficulty)
MIXED_JITTER = 0.12  # per-sample jitter around that base
MIXED_FIRST_P = {"fake-large": 0.85, "fake-small": 0.85}  # P(pick first-mentioned family)
UNKNOWN_CONF = (0.60, 0.24)  # small model facing a rare family it does not know
UNKNOWN_NEAR_P = 0.55  # P(guess the nearby common code) when unknown
NO_MATCH_CONF = (0.55, 0.10)
THINK_RECOVER_P = 0.95  # per row: thinking recovers the rare family
THINK_CONF = (0.88, 0.08)
THINK_TOKENS = (120, 240)  # extra output tokens per reply with thinking on: base + up to
HEDGED_CONF = (0.82, 0.06)  # listing hedges ("-style", "not included"): model is less sure
HEDGES = ("-style", "-shaped", "-print", "not included", "no electronics")
MALFORMED_P = {("fake-large", False): 0.0, ("fake-small", False): 0.06, ("fake-small", True): 0.02}

# keyword -> (tax code, rare?). The "model's knowledge", not the ground truth.
KNOWLEDGE: list[tuple[str, str, bool]] = [
    ("gift card", "CTX-900", True),
    ("voucher", "CTX-900", True),
    ("coin cell", "CTX-520", True),
    ("aa cells", "CTX-520", True),
    ("battery pack", "CTX-520", True),
    ("thermometer", "CTX-610", True),
    ("pressure cuff", "CTX-610", True),
    ("pulse meter", "CTX-610", True),
    ("headphones", "CTX-510", False),
    ("usb hub", "CTX-510", False),
    ("speaker", "CTX-510", False),
    ("smart plug", "CTX-510", False),
    ("ceramic mug", "CTX-100", False),
    ("throw pillow", "CTX-100", False),
    ("phone case", "CTX-100", False),
    ("plant pot", "CTX-100", False),
    ("paperback", "CTX-310", False),
    ("atlas", "CTX-310", False),
    ("cookbook", "CTX-310", False),
    ("granola", "CTX-210", False),
    ("olive oil", "CTX-210", False),
    ("herbal tea", "CTX-210", False),
    ("spice", "CTX-210", False),
    ("toddler", "CTX-420", False),
    ("kids", "CTX-420", False),
    ("rain jacket", "CTX-430", False),
    ("sneakers", "CTX-430", False),
    ("wool socks", "CTX-430", False),
]

# What a model that does not know a rare family tends to guess instead.
NEAR_MISS = {"CTX-900": "CTX-100", "CTX-520": "CTX-510", "CTX-610": "CTX-510"}
OTHER_GUESS = {"CTX-900": "CTX-310", "CTX-520": "CTX-100", "CTX-610": "CTX-100"}


# ----------------------------------------------------------------- helpers


def _u(*parts: object) -> float:
    """Uniform [0, 1) from a stable sha256 digest of the parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _thinking_on(request: dict[str, Any]) -> bool:
    t = request.get("thinking")
    if isinstance(t, dict):
        return t.get("type") not in (None, "disabled", "off")
    if isinstance(t, str):
        return t not in ("", "off", "disabled")
    return bool(t)


def _wants_structured(request: dict[str, Any]) -> bool:
    return str(request.get("output_mode", "text")) in ("json_schema", "tool")


def _listing(request: dict[str, Any]) -> str:
    msgs = request.get("messages") or []
    content = msgs[-1].get("content", "") if msgs else ""
    if isinstance(content, list):  # content blocks
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def _parse_listing(text: str) -> tuple[str, dict[str, str]]:
    """Title and specs from the 'Item: name | key: value | ...' line."""
    for line in text.splitlines():
        if line.lower().startswith("item:"):
            parts = [p.strip() for p in line.split(":", 1)[1].split("|")]
            title = parts[0]
            specs = {}
            for p in parts[1:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    specs[k.strip().lower()] = v.strip()
            return title, specs
    return text.strip()[:60], {}


def _matches(text: str) -> list[tuple[str, bool]]:
    low = text.lower()
    found: list[tuple[int, str, bool]] = []
    for kw, code, rare in KNOWLEDGE:
        pos = low.find(kw)
        if pos >= 0:
            found.append((pos, code, rare))
    found.sort()
    seen: list[tuple[str, bool]] = []
    for _, code, rare in found:
        if code not in [c for c, _ in seen]:
            seen.append((code, rare))
    return seen


def _conf(centre_spread: tuple[float, float], u: float) -> float:
    centre, spread = centre_spread
    return round(min(1.0, max(0.0, centre + spread * (u - 0.5))), 2)


# ----------------------------------------------------------------- the fake provider


def supports(candidate: dict[str, Any]) -> str | None:
    """Preflight: return a rejection reason, or None if the shape is served."""
    model = candidate.get("model")
    if model not in MODELS:
        return f"fake provider has no model {model!r} (known: {', '.join(MODELS)})"
    thinking = str(candidate.get("thinking", "off")) == "on"
    mode = str(candidate.get("output_mode", "text"))
    if model == "fake-small" and thinking and mode == "json_schema":
        return (
            "fake-small rejects json_schema structured output with thinking on "
            "(400: output_mode=json_schema is not supported with thinking for this model)"
        )
    return None


def respond(
    request: dict[str, Any], *, model: str, row_id: str, replica: int, sample: int
) -> dict[str, Any]:
    thinking = _thinking_on(request)
    key = (model, "think" if thinking else "plain", row_id, replica, sample)
    listing = _listing(request)
    title, specs = _parse_listing(listing)
    found = _matches(listing)

    # Map rare families the model does not know to a guess.
    knows_rare = model == "fake-large" or (
        thinking and _u(model, "recover", row_id) < THINK_RECOVER_P
    )
    effective: list[tuple[str, bool]] = []  # (code, unknown?)
    for code, rare in found:
        if rare and not knows_rare:
            near = _u(*key, "near") < UNKNOWN_NEAR_P
            effective.append((NEAR_MISS[code] if near else OTHER_GUESS[code], True))
        else:
            effective.append((code, False))

    u_conf = _u(*key, "conf")
    if not effective:
        code, conf = "CTX-100", _conf(NO_MATCH_CONF, u_conf)
    elif any(unknown for _, unknown in effective):
        code = next(c for c, unknown in effective if unknown)
        conf = _conf(UNKNOWN_CONF, u_conf)
    elif len(effective) >= 2:
        first = _u(*key, "pick") < MIXED_FIRST_P[model]
        code = effective[0][0] if first else effective[1][0]
        lo, hi = MIXED_BASE
        base = lo + (hi - lo) * _u("difficulty", listing)  # a property of the listing
        conf = _conf((base, MIXED_JITTER), u_conf)
    else:
        code = effective[0][0]
        if any(h in listing.lower() for h in HEDGES):
            conf = _conf(HEDGED_CONF, u_conf)
        elif thinking and found and found[0][1] and model != "fake-large":
            conf = _conf(THINK_CONF, u_conf)
        else:
            conf = _conf(CLEAR_CONF[model], u_conf)

    out = {
        "description": title[:1].upper() + title[1:],
        "specs": specs,
        "tax_code": code,
        "confidence": conf,
    }
    text = json.dumps(out, sort_keys=True)
    if _u(*key, "malformed") < MALFORMED_P.get((model, thinking), 0.0):
        text = text[: len(text) // 2]  # truncated mid-object

    system = request.get("system") or ""
    if isinstance(system, list):
        system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
    in_tok = (len(str(system)) + len(listing)) // 4
    out_tok = max(1, len(text) // 4)
    if thinking:
        out_tok += THINK_TOKENS[0] + int(THINK_TOKENS[1] * _u(*key, "think_tokens"))

    reply: dict[str, Any] = {
        "text": text,
        "served_model": model,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }
    if _wants_structured(request) and not text.endswith("}"):
        reply["parsed"] = None
    elif _wants_structured(request):
        reply["parsed"] = out
    return reply
