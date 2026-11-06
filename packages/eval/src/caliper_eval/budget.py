"""Budget guard: stop before a row would push spend over the limit.

Only spend made by the current invocation counts. Results already written stay
in the results file, so re-running the same command resumes where it stopped
(with a fresh budget for the remaining rows).
"""

from __future__ import annotations


class BudgetExceeded(RuntimeError):  # noqa: N818 - public name fixed by the contract
    def __init__(self, spent: float, limit: float, estimate: float) -> None:
        self.spent = spent
        self.limit = limit
        self.estimate = estimate
        super().__init__(
            f"budget exceeded: spent ${spent:.4f} + next row ~${estimate:.4f} > limit "
            f"${limit:.4f}. Results so far are saved; re-run to resume."
        )


class Budget:
    def __init__(self, limit_usd: float | None) -> None:
        if limit_usd is not None and limit_usd < 0:
            raise ValueError("budget limit must be >= 0")
        self.limit_usd = limit_usd
        self.spent = 0.0

    @property
    def remaining(self) -> float:
        if self.limit_usd is None:
            return float("inf")
        return max(0.0, self.limit_usd - self.spent)

    def check(self, estimate_usd: float = 0.0) -> None:
        """Raise BudgetExceeded if spending ``estimate_usd`` more would exceed the limit."""
        if self.limit_usd is None:
            return
        if self.spent >= self.limit_usd or self.spent + max(0.0, estimate_usd) > self.limit_usd:
            raise BudgetExceeded(self.spent, self.limit_usd, estimate_usd)

    def charge(self, usd: float) -> None:
        self.spent += max(0.0, usd)
