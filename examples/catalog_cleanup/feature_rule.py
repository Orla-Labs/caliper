"""The routing rule as it lives in the (fictional) production service.

This is the shape real feature code tends to have: it works on the raw reply
strings the HTTP client handed back (``None`` when the call failed), returns a
plain dict with the service's own action names, and was written by someone
who never heard of Caliper.

``decide.py`` is the mirror Caliper replays. ``tests/test_parity.py`` checks
the two agree on every reply sequence from a representative alphabet, so the
eval grades what production would actually have done.

Action names here -> Caliper outcomes:
    "publish" -> "auto_publish", "review" -> "needs_review", "manual" -> "fallback"
"""

from __future__ import annotations

import json
import math
from collections import Counter

PUBLISH_AT = 0.75
CODE_LIST = ("CTX-100 CTX-210 CTX-310 CTX-420 CTX-430 CTX-510 CTX-520 CTX-610 CTX-900").split()


def _read(raw: str | None) -> tuple[str, float] | None:
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if type(rec) is not dict:
        return None
    code, conf, desc = rec.get("tax_code"), rec.get("confidence"), rec.get("description")
    if code not in CODE_LIST or not isinstance(code, str):
        return None
    if type(conf) not in (int, float):  # rejects bools: type(True) is bool
        return None
    if math.isnan(conf) or conf < 0 or conf > 1:
        return None
    if type(desc) is not str or desc.strip() == "":
        return None
    return code, float(conf)


def route_listing(raw_replies: list[str | None]) -> dict:
    parsed = []
    for raw in raw_replies:
        item = _read(raw)
        if item is not None:
            parsed.append(item)

    if len(parsed) < 2:
        return {"action": "manual", "tax_code": None, "score": None}

    tally = Counter(code for code, _ in parsed)
    best = tally.most_common()
    if best[0][1] < 2 or (len(best) > 1 and best[1][1] == best[0][1]):
        return {"action": "review", "tax_code": None, "score": 0.0}

    winner = best[0][0]
    total = 0.0
    n = 0
    for code, conf in parsed:
        if code == winner:
            total += conf
            n += 1
    score = total / n
    action = "publish" if score >= PUBLISH_AT else "review"
    return {"action": action, "tax_code": winner, "score": round(score, 4)}
