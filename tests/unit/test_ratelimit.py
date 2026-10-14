from datetime import UTC, datetime

import pytest

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import RateLimitError
from uncorrelated_trading.ratelimit import DailyBudget, TokenBucket

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_bucket_blocks_fourth_acquire():
    clock = FakeClock(T0)
    bucket = TokenBucket(3, clock)
    for _ in range(4):
        bucket.acquire()
    assert (clock.now() - T0).total_seconds() >= 20
    assert (clock.now() - T0).total_seconds() == 60


def test_bucket_no_sleep_under_rate():
    clock = FakeClock(T0)
    bucket = TokenBucket(5, clock)
    for _ in range(5):
        bucket.acquire()
    assert clock.sleep_calls == []


def test_bucket_window_slides():
    clock = FakeClock(T0)
    bucket = TokenBucket(2, clock)
    bucket.acquire()
    clock.advance(30)
    bucket.acquire()
    bucket.acquire()  # must wait until the first one is 60 s old
    assert clock.now() == datetime(2026, 10, 13, 12, 1, tzinfo=UTC)


def test_bucket_rejects_bad_rate():
    with pytest.raises(ValueError):
        TokenBucket(0, FakeClock(T0))


def test_daily_budget():
    used = [0]
    budget = DailyBudget(
        limit=2, used_today=lambda: used[0], record=lambda n: used.__setitem__(0, used[0] + n)
    )
    assert budget.remaining() == 2
    budget.spend()
    budget.spend()
    assert budget.remaining() == 0
    with pytest.raises(RateLimitError) as exc:
        budget.spend()
    assert exc.value.provider == "twelvedata" and exc.value.retryable
    assert used[0] == 2


def test_daily_budget_multi_credit_spend_does_not_partially_record():
    used = [7]
    budget = DailyBudget(10, lambda: used[0], lambda n: used.__setitem__(0, used[0] + n))
    with pytest.raises(RateLimitError):
        budget.spend(4)
    assert used[0] == 7
    budget.spend(3)
    assert used[0] == 10


def test_daily_budget_remaining_never_negative_and_rejects_negative_spend():
    budget = DailyBudget(5, lambda: 9, lambda n: None)
    assert budget.remaining() == 0
    with pytest.raises(ValueError):
        budget.spend(-1)
