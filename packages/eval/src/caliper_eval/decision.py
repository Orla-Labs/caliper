"""Decision wrapper: grade the *system outcome*, not single replies.

A real feature wraps the model in logic (vote loops, thresholds, fallbacks).
The eval replays that logic as ``outcome = decide(replies)``. Two helpers:

* :func:`decision` - decorator that lets ``decide`` return a plain ``str``,
  a ``(outcome, score)`` tuple, a ``dict`` of Decision fields or a
  :class:`~caliper_eval.types.Decision`; the wrapped function always returns
  a ``Decision``.
* :func:`assert_parity` - exhaustively checks that the mirrored rule used in
  the eval agrees with the real production rule over every reply sequence
  built from a finite alphabet of representative replies.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .types import Decision, Reply

DEFAULT_MAX_SEQUENCES = 100_000


class ParityError(AssertionError):
    """The mirrored decision rule disagrees with the real one."""

    def __init__(
        self,
        replies: list[Reply],
        mirror_outcome: str | None,
        real_outcome: str | None,
        note: str | None = None,
    ) -> None:
        self.replies = replies
        self.mirror_outcome = mirror_outcome
        self.real_outcome = real_outcome
        seq = [_describe(r) for r in replies]
        msg = (
            f"decision parity failed for replies {seq}: "
            f"mirror={mirror_outcome!r} real={real_outcome!r}"
        )
        if note:
            msg += f" ({note})"
        super().__init__(msg)


def _describe(reply: Reply) -> Any:
    if reply.error is not None:
        return f"<error: {reply.error}>"
    if reply.parsed is not None:
        return reply.parsed
    return reply.text


def coerce_decision(value: Any) -> Decision:
    """Turn a ``decide`` return value into a :class:`Decision`."""
    if isinstance(value, Decision):
        return value
    if isinstance(value, str):
        return Decision(outcome=value)
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        score = value[1]
        return Decision(outcome=value[0], score=None if score is None else float(score))
    if isinstance(value, dict):
        return Decision.model_validate(value)
    raise TypeError(
        "decide() must return a str, a (outcome, score) tuple, a dict or a Decision; "
        f"got {type(value).__name__}"
    )


def decision(fn: Callable[[list[Reply]], Any]) -> Callable[[list[Reply]], Decision]:
    """Decorator: normalize a ``decide(replies)`` function's return value."""

    @functools.wraps(fn)
    def wrapper(replies: list[Reply]) -> Decision:
        return coerce_decision(fn(replies))

    wrapper.__caliper_decision__ = True  # type: ignore[attr-defined]
    return wrapper


def _as_reply(item: Reply | str) -> Reply:
    if isinstance(item, Reply):
        return item
    if isinstance(item, str):
        return Reply(text=item)
    raise TypeError(f"alphabet items must be Reply or str, got {type(item).__name__}")


def _outcome_of(value: Any) -> str:
    return coerce_decision(value).outcome


def assert_parity(
    mirror: Callable[[list[Reply]], Any],
    real: Callable[..., Any],
    alphabet: Sequence[Reply | str],
    *,
    length: int | Iterable[int],
    adapter: Callable[[list[Reply]], Any] | None = None,
    outcome_of: Callable[[Any], Any] | None = None,
    max_sequences: int = DEFAULT_MAX_SEQUENCES,
) -> int:
    """Check ``mirror`` against ``real`` over every reply sequence.

    Enumerates ``itertools.product(alphabet, repeat=L)`` for each ``L`` in
    ``length`` (an int or an iterable of ints, for loops that stop early).
    ``real`` is called with ``adapter(replies)`` when an adapter is given
    (e.g. ``lambda rs: [r.text for r in rs]`` for a rule that takes raw
    texts), else with the replies. Both sides may return anything
    :func:`coerce_decision` accepts; outcomes are compared. When the real
    rule returns something else (an Enum, a bool), pass ``outcome_of`` to map
    its return value first (e.g. ``lambda v: v.value``).

    Raises :class:`ParityError` on the first counterexample (exceptions on
    either side count as a mismatch unless both raise the same type).
    Returns the number of sequences checked. Raises ``ValueError`` before
    enumerating when the total exceeds ``max_sequences``.
    """
    replies_alphabet = [_as_reply(a) for a in alphabet]
    if not replies_alphabet:
        raise ValueError("alphabet must not be empty")
    lengths = [length] if isinstance(length, int) else list(length)
    if not lengths or any(n < 0 for n in lengths):
        raise ValueError(f"length must be a non-negative int or ints, got {length!r}")
    total = sum(len(replies_alphabet) ** n for n in lengths)
    if total > max_sequences:
        raise ValueError(
            f"parity check would enumerate {total} sequences "
            f"(alphabet={len(replies_alphabet)}, lengths={lengths}), above "
            f"max_sequences={max_sequences}; shrink the alphabet to representative "
            "replies or raise max_sequences"
        )

    checked = 0
    for n in lengths:
        for combo in itertools.product(replies_alphabet, repeat=n):
            replies = list(combo)
            m_out, m_exc = _safe_outcome(mirror, replies)
            real_arg = adapter(replies) if adapter is not None else replies
            r_out, r_exc = _safe_outcome(real, real_arg, outcome_of)
            checked += 1
            if m_exc is not None or r_exc is not None:
                if m_exc is not None and r_exc is not None and type(m_exc) is type(r_exc):
                    continue
                note = "; ".join(
                    f"{side} raised {type(e).__name__}: {e}"
                    for side, e in (("mirror", m_exc), ("real", r_exc))
                    if e is not None
                )
                raise ParityError(replies, m_out, r_out, note)
            if m_out != r_out:
                raise ParityError(replies, m_out, r_out)
    return checked


def _safe_outcome(
    fn: Callable[..., Any], arg: Any, mapper: Callable[[Any], Any] | None = None
) -> tuple[str | None, Exception | None]:
    try:
        value = fn(arg)
        if mapper is not None:
            value = mapper(value)
        return _outcome_of(value), None
    except Exception as exc:  # user code under test
        return None, exc


__all__ = [
    "DEFAULT_MAX_SEQUENCES",
    "ParityError",
    "assert_parity",
    "coerce_decision",
    "decision",
]
