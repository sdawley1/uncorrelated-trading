from datetime import UTC, datetime

import pytest

from uncorrelated_trading.clock import FakeClock, SystemClock


def test_system_clock_is_utc_aware():
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0


def test_fake_clock_sleep_advances_and_records():
    c = FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    c.sleep(90)
    assert c.now() == datetime(2026, 10, 13, 12, 1, 30, tzinfo=UTC)
    assert c.sleep_calls == [90]


def test_fake_clock_rejects_naive():
    with pytest.raises(ValueError, match="tz-aware"):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001
