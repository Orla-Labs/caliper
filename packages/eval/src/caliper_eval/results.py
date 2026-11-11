"""Resumable, append-only JSONL results store. One RowResult per line.

Crash safety: each line is written and flushed on its own. If a crash leaves a
truncated last line, ``load`` ignores it and the next ``append`` cuts it off
before writing, so a re-run simply redoes that row.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from caliper_eval.types import RowResult, canonical_json


class ResultsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._repaired = False

    # ------------------------------------------------------------------ reading

    def load(self) -> list[RowResult]:
        """All results in file order. Later lines for the same key win in ``latest``."""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        complete_tail = data.endswith(b"\n")
        out: list[RowResult] = []
        for i, raw in enumerate(lines):
            if not raw.strip():
                continue
            is_last = i == len(lines) - 1
            try:
                out.append(RowResult.model_validate(json.loads(raw)))
            except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
                if is_last and not complete_tail:
                    break  # truncated by a crash mid-write; ignored
                raise ValueError(f"{self.path}:{i + 1}: corrupt result line: {exc}") from exc
        return out

    def latest(self) -> dict[tuple[str, str, int], RowResult]:
        """key -> most recent result for that key."""
        return {r.key: r for r in self.load()}

    def done_keys(self, *, include_errors: bool = True) -> set[tuple[str, str, int]]:
        """Keys already stored. With ``include_errors=False``, rows whose latest
        result has a row error or any reply error are not counted as done."""
        latest = self.latest()
        if include_errors:
            return set(latest)
        return {k for k, r in latest.items() if not has_error(r)}

    # ------------------------------------------------------------------ writing

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        self._repaired = True

    def _repair_tail(self) -> None:
        if self._repaired:
            return
        self._repaired = True
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n") + 1  # 0 when there is no complete line at all
            with self.path.open("r+b") as fh:
                fh.truncate(cut)

    def append(self, result: RowResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_tail()
        line = canonical_json(result.model_dump(mode="json")) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()


def has_error(result: RowResult) -> bool:
    return result.error is not None or any(r.error is not None for r in result.replies)
