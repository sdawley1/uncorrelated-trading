"""Clock abstraction so time-dependent code can be tested deterministically."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Source of the current UTC instant and a way to sleep."""

    def now(self) -> datetime:
        """Return the current instant as a tz-aware UTC datetime."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds`` seconds."""
        ...


class SystemClock:
    """Real wall clock backed by :func:`datetime.now` and :func:`time.sleep`."""

    def now(self) -> datetime:
        """Return the current UTC instant."""
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` seconds (no-op for non-positive values)."""
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests; ``sleep`` advances time instead of blocking."""

    def __init__(self, start: datetime) -> None:
        """Start the clock at ``start`` (must be tz-aware)."""
        if start.tzinfo is None:
            msg = "FakeClock start must be tz-aware"
            raise ValueError(msg)
        self._now = start.astimezone(UTC)
        self.sleep_calls: list[float] = []

    def now(self) -> datetime:
        """Return the current fake instant."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Record the call and advance the clock by ``seconds``."""
        self.sleep_calls.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now += timedelta(seconds=seconds)
