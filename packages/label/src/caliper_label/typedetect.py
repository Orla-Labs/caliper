"""Guess a card display type for each column from its engine type and a sample of values.

Order of checks for string-ish columns: image (URL, s3/gs key or local path with an
image extension) > link (http URL) > json (object/array text) > badge (few distinct
short values) > long_text (long or multi-line) > text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from .project import DisplayType

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp")
_NUMERIC = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|UHUGEINT|"
    r"FLOAT|DOUBLE|REAL|DECIMAL|NUMERIC|INT\d*)\b",
    re.IGNORECASE,
)
_NESTED = re.compile(r"^(STRUCT|MAP|JSON|UNION)\b|\[\]$|^LIST\b", re.IGNORECASE)
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_OBJECT_URL = re.compile(r"^(s3|gs|gcs|r2)://\S+$", re.IGNORECASE)

BADGE_MAX_DISTINCT = 12
BADGE_MAX_LEN = 40
LONG_TEXT_LEN = 120


def _strip_query(s: str) -> str:
    return s.split("?", 1)[0].split("#", 1)[0]


def is_image_ref(v: Any) -> bool:
    """URL, object-store key or local path that ends in an image extension."""
    if not isinstance(v, str) or not v or len(v) > 2048 or "\n" in v:
        return False
    return _strip_query(v).lower().endswith(IMAGE_EXTENSIONS)


def _looks_json(v: str) -> bool:
    s = v.strip()
    if not (s.startswith("{") and s.endswith("}")) and not (s.startswith("[") and s.endswith("]")):
        return False
    try:
        return isinstance(json.loads(s), dict | list)
    except ValueError:
        return False


def detect_display(dtype: str, values: Iterable[Any]) -> DisplayType:
    vals = [v for v in values if v is not None and v != ""]
    dt = (dtype or "").strip()
    if dt.upper().startswith("BOOL"):
        return DisplayType.BADGE
    if _NUMERIC.match(dt):
        return DisplayType.NUMBER
    if _NESTED.search(dt):
        return DisplayType.JSON
    if not vals:
        return DisplayType.TEXT
    # Untyped sources (plugins): infer from Python values.
    if all(isinstance(v, bool) for v in vals):
        return DisplayType.BADGE
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
        return DisplayType.NUMBER
    if any(isinstance(v, dict | list) for v in vals):
        return DisplayType.JSON
    strs = [str(v) for v in vals]
    if all(is_image_ref(s) for s in strs):
        return DisplayType.IMAGE
    if all(_URL.match(s) for s in strs):
        return DisplayType.LINK
    if all(_OBJECT_URL.match(s) for s in strs):
        return DisplayType.LINK
    if all(_looks_json(s) for s in strs):
        return DisplayType.JSON
    distinct = set(strs)
    max_len = max(len(s) for s in strs)
    if max_len > LONG_TEXT_LEN or any("\n" in s for s in strs):
        return DisplayType.LONG_TEXT
    if (
        len(strs) >= 4
        and len(distinct) <= BADGE_MAX_DISTINCT
        and len(distinct) <= len(strs) / 2
        and max_len <= BADGE_MAX_LEN
    ):
        return DisplayType.BADGE
    return DisplayType.TEXT


def detect_columns(columns: Sequence[tuple[str, str]], rows: Sequence[dict[str, Any]]):
    """``[(name, dtype)]`` + sample rows -> ``list[Column]``."""
    from .connectors.base import Column

    return [
        Column(name=name, dtype=dtype, display=detect_display(dtype, (r.get(name) for r in rows)))
        for name, dtype in columns
    ]
