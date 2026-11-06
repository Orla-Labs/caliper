"""Run candidates over a golden set and record one RowResult per
(candidate, row, replica).

Order is deterministic: candidates in config order, then replica, then golden
row order, then sample. The reference candidate is replayed
``config.noise_replays`` times (replicas 0..n-1) for the noise floor; other
candidates run replica 0 only.

Each row sends ``candidate.replies_per_row`` requests (``sample`` 0..k-1), then
calls ``decide(replies)``. ``decide`` sees replies with errors too, because the
real feature's fallback logic is part of what is being evaluated. A ``decide``
exception becomes ``RowResult.error``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from caliper_eval.budget import Budget
from caliper_eval.config import EvalConfig
from caliper_eval.loader import load_golden
from caliper_eval.providers import get_provider
from caliper_eval.providers.base import error_text
from caliper_eval.results import ResultsStore, has_error
from caliper_eval.types import (
    Candidate,
    DecideFn,
    Decision,
    GoldenRow,
    Reply,
    RowResult,
    candidate_sha256,
    request_sha256,
)

ProgressFn = Callable[[RowResult, int, int], None]  # (result, done, total)
Key = tuple[str, str, int]


def plan_keys(config: EvalConfig, rows: list[GoldenRow]) -> list[Key]:
    """Every (candidate, row_id, replica) the run covers, in execution order."""
    keys: list[Key] = []
    for c in config.candidates:
        for replica in range(config.replicas_for(c.name)):
            keys.extend((c.name, row.id, replica) for row in rows)
    return keys


def default_run_id(config: EvalConfig, rows: list[GoldenRow]) -> str:
    """Content-derived id from what produces replies: the candidates and the
    golden set. Budget, CI or cost settings do not change it."""
    h = hashlib.sha256()
    for c in config.candidates:
        h.update(candidate_sha256(c).encode())
    for row in rows:
        h.update(row.id.encode())
        h.update(request_sha256(row.request).encode())
    return h.hexdigest()[:12]


def is_current(stored: RowResult, candidate: Candidate, row: GoldenRow) -> bool:
    """A stored result is reusable only if it came from the same request and the
    same candidate settings (a renamed-in-place model or edited row is stale)."""
    return stored.request_sha256 == request_sha256(
        row.request
    ) and stored.candidate_sha256 == candidate_sha256(candidate)


def coerce_decision(value: Any) -> Decision:
    if isinstance(value, Decision):
        return value
    if isinstance(value, str):
        return Decision(outcome=value)
    if isinstance(value, dict):
        return Decision.model_validate(value)
    raise TypeError(f"decide() must return Decision, str or dict, got {type(value).__name__}")


def run_row(
    provider,
    candidate: Candidate,
    row: GoldenRow,
    replica: int,
    decide: DecideFn,
    run_id: str,
) -> RowResult:
    """Send ``replies_per_row`` requests for one row and decide the outcome."""
    replies: list[Reply] = []
    for sample in range(candidate.replies_per_row):
        try:
            reply = provider.send(
                row.request, candidate, row_id=row.id, replica=replica, sample=sample
            )
        except Exception as exc:  # providers should not raise; belt and braces
            reply = Reply(error=error_text(exc))
        replies.append(reply)
    decision: Decision | None = None
    error: str | None = None
    try:
        decision = coerce_decision(decide(replies))
    except Exception as exc:
        error = f"decide() raised {error_text(exc)}"
    return RowResult(
        run_id=run_id,
        candidate=candidate.name,
        row_id=row.id,
        replica=replica,
        request_sha256=request_sha256(row.request),
        candidate_sha256=candidate_sha256(candidate),
        replies=replies,
        decision=decision,
        error=error,
    )


def row_cost(result: RowResult) -> float:
    return sum(r.cost_usd for r in result.replies)


def run_eval(
    config: EvalConfig,
    *,
    results_path: str | Path,
    budget: Budget | None = None,
    resume: bool = True,
    decide: DecideFn | None = None,
    progress: ProgressFn | None = None,
    retry_errors: bool = False,
    rows: list[GoldenRow] | None = None,
    run_id: str | None = None,
) -> list[RowResult]:
    """Run the eval and return results for every planned key, in plan order.

    * ``resume``: skip keys already in the results file (else start it fresh).
      A stored row is reused only if its request and candidate settings hashes
      match; otherwise it is re-run and the new line supersedes it. Changes to
      ``decide`` or to a fake script's code are NOT detected: use
      ``resume=False`` (or re-decide from stored replies) after editing them.
    * ``retry_errors``: with resume, also redo rows whose stored result has an
      error (a newer line for the same key supersedes the old one).
    * ``budget``: defaults to ``Budget(config.budget_usd)``. Raises
      ``BudgetExceeded`` before a row whose estimated cost (mean stored cost per
      row for that candidate) would exceed the limit; finished rows are kept.
    """
    rows = rows if rows is not None else load_golden(config.golden_path)
    decide = decide or config.load_decide()
    budget = budget if budget is not None else Budget(config.budget_usd)
    run_id = run_id or default_run_id(config, rows)
    store = ResultsStore(results_path)
    if not resume:
        store.reset()
    latest = store.latest()
    by_id = {row.id: row for row in rows}
    keys = plan_keys(config, rows)
    done: set[Key] = set()
    if resume:
        for key in keys:
            stored = latest.get(key)
            if stored is None or (retry_errors and has_error(stored)):
                continue
            if is_current(stored, config.candidate(key[0]), by_id[key[1]]):
                done.add(key)
    total = len(keys)
    # per-candidate [cost, count] of known rows, for the budget estimate
    spend: dict[str, list[float]] = {}
    for key, r in latest.items():
        acc = spend.setdefault(key[0], [0.0, 0])
        acc[0] += row_cost(r)
        acc[1] += 1

    providers = {
        c.name: get_provider(c, base_dir=config.config_dir, pricing=config.pricing)
        for c in config.candidates
    }
    finished = sum(1 for k in keys if k in done)
    for cand_name, row_id, replica in keys:
        if (cand_name, row_id, replica) in done:
            continue
        candidate = config.candidate(cand_name)
        acc = spend.setdefault(cand_name, [0.0, 0])
        estimate = acc[0] / acc[1] if acc[1] else 0.0
        budget.check(estimate)
        result = run_row(providers[cand_name], candidate, by_id[row_id], replica, decide, run_id)
        store.append(result)
        latest[result.key] = result
        cost = row_cost(result)
        budget.charge(cost)
        acc[0] += cost
        acc[1] += 1
        finished += 1
        if progress is not None:
            progress(result, finished, total)

    return [latest[k] for k in keys if k in latest]
