from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.ratelimit import TokenBucket

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@settings(max_examples=100, deadline=None)
@given(
    rate=st.integers(min_value=1, max_value=10),
    gaps=st.lists(st.floats(min_value=0.0, max_value=40.0, allow_nan=False), max_size=60),
)
def test_never_more_than_rate_in_any_60s_window(rate, gaps):
    clock = FakeClock(T0)
    bucket = TokenBucket(rate, clock)
    stamps: list[datetime] = []
    for gap in gaps:
        clock.advance(gap)
        before = clock.now()
        bucket.acquire()
        stamps.append(clock.now())
        assert clock.now() >= before
    assert stamps == sorted(stamps)
    # Half-open windows [t, t + 60 s): acquisitions i and i + rate are >= 60 s apart.
    for i in range(len(stamps) - rate):
        assert stamps[i + rate] - stamps[i] >= timedelta(seconds=60)


@settings(max_examples=50, deadline=None)
@given(rate=st.integers(min_value=1, max_value=10), n=st.integers(min_value=0, max_value=40))
def test_burst_takes_minimal_time(rate, n):
    clock = FakeClock(T0)
    bucket = TokenBucket(rate, clock)
    for _ in range(n):
        bucket.acquire()
    # A back-to-back burst of n acquisitions finishes at floor((n - 1) / rate) minutes.
    expected = 60 * ((n - 1) // rate) if n else 0
    assert (clock.now() - T0).total_seconds() == expected
