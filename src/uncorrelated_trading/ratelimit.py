"""Client-side rate limiting: a sliding-window token bucket and a persisted daily budget.

:class:`TokenBucket` enforces a per-minute request rate (Twelve Data free tier: 8/min) by
blocking through a :class:`~uncorrelated_trading.clock.Clock`, so tests run instantly with a
``FakeClock``. :class:`DailyBudget` enforces a per-day credit limit whose counter lives in the
store's ``api_usage`` table (injected as two callables).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.errors import RateLimitError

_WINDOW = timedelta(seconds=60)
_MAX_WAITS = 1000  # bound on sleep iterations in one acquire()


class TokenBucket:
    """Sliding-window limiter: at most ``rate_per_minute`` acquisitions in any 60 s window."""

    def __init__(self, rate_per_minute: int, clock: Clock) -> None:
        """Create the limiter.

        Parameters
        ----------
        rate_per_minute : int
            Maximum acquisitions in any half-open 60-second window (must be >= 1).
        clock : Clock
            Time source; ``clock.sleep`` is used to block.

        Raises
        ------
        ValueError
            If ``rate_per_minute < 1``.
        """
        if rate_per_minute < 1:
            msg = f"rate_per_minute must be >= 1, got {rate_per_minute}"
            raise ValueError(msg)
        self._rate = rate_per_minute
        self._clock = clock
        self._stamps: deque[datetime] = deque()

    def acquire(self) -> None:
        """Block via ``clock.sleep`` until a slot is free, then take it.

        Raises
        ------
        RuntimeError
            If the clock fails to advance after many sleeps (a broken clock).
        """
        for _ in range(_MAX_WAITS):
            now = self._clock.now()
            while self._stamps and now - self._stamps[0] >= _WINDOW:
                self._stamps.popleft()
            if len(self._stamps) < self._rate:
                self._stamps.append(now)
                return
            wait = (self._stamps[0] + _WINDOW - now).total_seconds()
            self._clock.sleep(max(wait, 1e-6))
        msg = "TokenBucket.acquire: clock did not advance"
        raise RuntimeError(msg)


class DailyBudget:
    """Per-day credit limit for the Twelve Data provider."""

    def __init__(
        self, limit: int, used_today: Callable[[], int], record: Callable[[int], None]
    ) -> None:
        """Create the budget.

        Parameters
        ----------
        limit : int
            Credits allowed per day.
        used_today : Callable[[], int]
            Returns credits already spent today (``BarStore.api_usage_today`` bound to the
            provider and day).
        record : Callable[[int], None]
            Persists ``n`` newly spent credits (``BarStore.add_api_usage`` bound likewise).
        """
        self._limit = limit
        self._used_today = used_today
        self._record = record

    def remaining(self) -> int:
        """Return the credits still available today (never negative)."""
        return max(0, self._limit - self._used_today())

    def spend(self, n: int = 1) -> None:
        """Spend ``n`` credits, or raise without recording anything if that would exceed it.

        Parameters
        ----------
        n : int
            Credits to spend (>= 0).

        Raises
        ------
        RateLimitError
            With provider ``"twelvedata"`` when ``used_today() + n > limit``.
        ValueError
            If ``n`` is negative.
        """
        if n < 0:
            msg = f"cannot spend a negative number of credits: {n}"
            raise ValueError(msg)
        used = self._used_today()
        if used + n > self._limit:
            raise RateLimitError(
                "twelvedata", f"daily budget exhausted ({used}/{self._limit} credits used)"
            )
        self._record(n)
