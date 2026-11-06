"""Rare-class stratified sampler: build a golden set from production logs.

Rare strata (e.g. ``positive`` / ``unsure``) are kept whole (optionally
capped per stratum); everything else is pooled as "negatives" and sampled
down to ``negatives`` rows. Sampling is ``random.Random(seed)`` over line
indices; selected rows are emitted in **log order**, so the output is
byte-stable for a given input + seed.

Each output row keeps the logged request verbatim (key order preserved; the
results hash is key-order independent anyway) and records its ``stratum``.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .types import GoldenRow

MISSING = object()


class SampleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_lines: int
    n_selected: int
    n_skipped: int  # unparseable line, missing/non-object request, missing stratum
    by_stratum: dict[str, int] = Field(default_factory=dict)  # available rows
    selected_by_stratum: dict[str, int] = Field(default_factory=dict)
    skipped_reasons: dict[str, int] = Field(default_factory=dict)


def get_path(obj: Any, path: str) -> Any:
    """Dotted field path lookup (``a.b.0.c``); returns ``MISSING`` if absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def _stratum_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def stratified_sample(
    lines: Iterable[str],
    *,
    stratum_field: str,
    keep: Sequence[str],
    negatives: int,
    request_field: str = "request",
    id_field: str | None = None,
    reference_field: str | None = None,
    cap_per_stratum: int | None = None,
    seed: int = 0,
) -> tuple[list[GoldenRow], SampleSummary]:
    keep_set = set(keep)
    parsed: list[tuple[int, str, str, dict[str, Any], dict[str, Any]]] = []
    skipped: dict[str, int] = {}
    n_lines = 0
    for lineno, raw in enumerate(lines):
        if not raw.strip():
            continue
        n_lines += 1
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            skipped["invalid json"] = skipped.get("invalid json", 0) + 1
            continue
        request = get_path(rec, request_field)
        if not isinstance(request, dict):
            skipped["missing or non-object request"] = (
                skipped.get("missing or non-object request", 0) + 1
            )
            continue
        stratum = get_path(rec, stratum_field)
        if stratum is MISSING or stratum is None:
            skipped["missing stratum"] = skipped.get("missing stratum", 0) + 1
            continue
        row_id = f"row-{lineno:06d}"
        if id_field is not None:
            rid = get_path(rec, id_field)
            if rid is not MISSING and rid is not None:
                row_id = str(rid)
        parsed.append((lineno, row_id, _stratum_str(stratum), request, rec))

    by_stratum: dict[str, list[int]] = {}
    for i, (_, _, stratum, _, _) in enumerate(parsed):
        by_stratum.setdefault(stratum, []).append(i)

    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not crypto
    chosen: set[int] = set()
    for stratum in sorted(by_stratum):
        if stratum not in keep_set:
            continue
        members = by_stratum[stratum]
        if cap_per_stratum is not None and len(members) > cap_per_stratum:
            members = rng.sample(members, cap_per_stratum)
        chosen.update(members)
    pool = [i for s in sorted(by_stratum) if s not in keep_set for i in by_stratum[s]]
    pool.sort()
    if negatives < len(pool):
        pool = rng.sample(pool, max(negatives, 0))
    chosen.update(pool)

    rows: list[GoldenRow] = []
    seen_ids: set[str] = set()
    selected_by: dict[str, int] = {}
    for i in sorted(chosen):
        _, row_id, stratum, request, rec = parsed[i]
        if row_id in seen_ids:
            raise ValueError(f"duplicate row id {row_id!r} from --id-field {id_field!r}")
        seen_ids.add(row_id)
        ref = None
        if reference_field is not None:
            val = get_path(rec, reference_field)
            ref = None if val is MISSING or val is None else _stratum_str(val)
        rows.append(GoldenRow(id=row_id, request=request, stratum=stratum, reference_outcome=ref))
        selected_by[stratum] = selected_by.get(stratum, 0) + 1

    summary = SampleSummary(
        n_lines=n_lines,
        n_selected=len(rows),
        n_skipped=sum(skipped.values()),
        by_stratum={s: len(v) for s, v in sorted(by_stratum.items())},
        selected_by_stratum=dict(sorted(selected_by.items())),
        skipped_reasons=dict(sorted(skipped.items())),
    )
    return rows, summary


def golden_jsonl(rows: Iterable[GoldenRow]) -> str:
    """Serialize golden rows; request key order is preserved (no sort_keys)."""
    out = []
    for row in rows:
        data = row.model_dump(mode="json", exclude_defaults=True)
        data.setdefault("request", row.request)
        out.append(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "".join(line + "\n" for line in out)


__all__ = ["SampleSummary", "get_path", "golden_jsonl", "stratified_sample"]
