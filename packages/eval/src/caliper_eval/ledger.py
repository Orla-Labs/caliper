"""Test-split ledger: the test set is touched once per candidate, and every
touch is recorded.

A *touch* is a completed ``caliper eval`` run that reports a candidate on
rows with ``split == "test"``. Each touch appends one JSON line to the ledger
(default ``OUT/test_ledger.jsonl``, or ``--test-ledger PATH``)::

    {"candidate": "small", "candidate_sha256": "...", "test_set_sha256": "...",
     "n_test_rows": 40, "reference": false, "touched_at": "2026-11-06T12:00:00Z"}

* ``candidate_sha256`` is the candidate's settings hash (model, thinking,
  output mode, params, ...), the same one stale-resume uses.
* ``test_set_sha256`` hashes the test rows (id, request, labels), so adding
  val rows does not reset it but changing a test row does.
* A **re-touch** is a touch of a non-reference candidate whose
  (``candidate_sha256``, ``test_set_sha256``) is already in the ledger. It is
  a warning on stderr; under ``--ci`` it fails that candidate unless
  ``--allow-test-retouch``. The reference is recorded but never a re-touch: it
  is re-measured on every run and nothing is selected for it.
* The timestamp lives only in the ledger, never on stdout or in report.json,
  so reports stay byte-deterministic.
* ``--no-resume`` does not clear the ledger. A fresh ``--out`` per CI job
  starts an empty ledger; point ``--test-ledger`` at a persisted file to
  detect re-touches across jobs.
* Golden sets with no ``test`` rows record nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .types import Candidate, GoldenRow, Split, candidate_sha256, canonical_json


class TestTouch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__ = False  # not a pytest class

    candidate: str
    candidate_sha256: str
    test_set_sha256: str
    n_test_rows: int
    reference: bool
    touched_at: str


class Retouch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    previous_touches: int


def split_sha256(golden: Iterable[GoldenRow]) -> str | None:
    """Hash of the test rows (sorted by id); ``None`` when there are none."""
    rows = sorted((g for g in golden if g.split == Split.TEST), key=lambda g: g.id)
    if not rows:
        return None
    h = hashlib.sha256()
    for g in rows:
        h.update(
            canonical_json(
                g.model_dump(
                    mode="json",
                    include={"id", "request", "label", "label_approved", "label_source"},
                )
            ).encode("utf-8")
        )
    return h.hexdigest()


def read_ledger(path: str | Path) -> list[TestTouch]:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(TestTouch.model_validate_json(line))
    return out


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_touches(
    path: str | Path,
    candidates: Sequence[Candidate],
    golden: Sequence[GoldenRow],
    reference: str,
    *,
    clock: Callable[[], str] = _now,
) -> list[Retouch]:
    """Append one touch per candidate; return the non-reference re-touches.

    Does nothing (and returns ``[]``) when the golden set has no test rows.
    """
    test_hash = split_sha256(golden)
    if test_hash is None:
        return []
    n_test = sum(1 for g in golden if g.split == Split.TEST)
    previous = read_ledger(path)
    seen: dict[tuple[str, str], int] = {}
    for t in previous:
        key = (t.candidate_sha256, t.test_set_sha256)
        seen[key] = seen.get(key, 0) + 1
    retouches: list[Retouch] = []
    stamp = clock()
    lines = []
    for c in candidates:
        sha = candidate_sha256(c)
        is_ref = c.name == reference
        n_prev = seen.get((sha, test_hash), 0)
        if n_prev and not is_ref:
            retouches.append(Retouch(candidate=c.name, previous_touches=n_prev))
        touch = TestTouch(
            candidate=c.name,
            candidate_sha256=sha,
            test_set_sha256=test_hash,
            n_test_rows=n_test,
            reference=is_ref,
            touched_at=stamp,
        )
        lines.append(json.dumps(touch.model_dump(mode="json"), sort_keys=True) + "\n")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)
    return retouches


__all__ = ["Retouch", "TestTouch", "read_ledger", "record_touches", "split_sha256"]
