from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.scheduler import daily_at

instants = st.datetimes(
    min_value=datetime(2020, 1, 1),  # noqa: DTZ001
    max_value=datetime(2030, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)
zones = st.sampled_from(
    [
        "America/New_York",
        "America/Chicago",
        "Europe/London",
        "Asia/Tokyo",
        "Australia/Sydney",
        "UTC",
    ]
)
wall_times = st.times()
rules = st.sampled_from(
    [
        lambda d: True,
        lambda d: d.weekday() < 5,
        lambda d: d.weekday() == 2,
        lambda d: d.day % 3 == 0,
    ]
)


def _wall_time_exists(d: date, t: time, tz: str) -> bool:
    zone = ZoneInfo(tz)
    local = datetime.combine(d, t, zone)
    back = local.astimezone(UTC).astimezone(zone)
    return back.replace(tzinfo=None) == local.replace(tzinfo=None)


@settings(max_examples=100, deadline=None)
@given(now=instants, t=wall_times, tz=zones, allowed=rules)
def test_daily_at_is_after_now_on_allowed_date_at_wall_time(now, t, tz, allowed):
    fire = daily_at(t, tz, allowed)(now)
    assert fire.tzinfo is not None and fire.utcoffset().total_seconds() == 0
    assert fire > now
    local = fire.astimezone(ZoneInfo(tz))
    assert allowed(local.date())
    if _wall_time_exists(local.date(), t, tz):
        assert local.time() == t
    # Nothing is skipped: every earlier allowed date's fire instant is not after now.
    zone = ZoneInfo(tz)
    d = now.astimezone(zone).date() - timedelta(days=1)
    while d < local.date():
        if allowed(d):
            assert datetime.combine(d, t, zone) <= now
        d += timedelta(days=1)
