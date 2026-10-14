from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.calendar import StaticCalendar, data_request_end, resolve_as_of

NY = ZoneInfo("America/New_York")
CAL = StaticCalendar()

instants = st.datetimes(
    min_value=datetime(2020, 1, 1),  # noqa: DTZ001
    max_value=datetime(2030, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)


@settings(max_examples=100, deadline=None)
@given(now=instants, settle=st.integers(min_value=0, max_value=120))
def test_as_of_day_is_trading_day_not_after_local_date(now, settle):
    a = resolve_as_of(now, CAL, "America/New_York", settle)
    local = now.astimezone(NY).date()
    assert a.day <= local
    assert [d.day for d in CAL.trading_days(a.day, a.day)] == [a.day]
    assert a.provisional == (a.mode == "post_close_settling")
    if a.mode in ("post_close", "post_close_settling"):
        assert a.day == local
    else:
        assert a.day < local
    # No trading day strictly between the as-of day and the local date, except today when
    # its session has not settled.
    between = [d.day for d in CAL.trading_days(a.day + timedelta(days=1), local)]
    assert between in ([], [local])


@settings(max_examples=100, deadline=None)
@given(now=instants)
def test_request_end_respects_guard(now):
    a = resolve_as_of(now, CAL, "America/New_York", 20)
    end = data_request_end(a, "America/New_York", now)
    assert end <= now - timedelta(minutes=16)
    assert end.astimezone(NY).date() <= a.day


@settings(max_examples=30, deadline=None)
@given(year=st.integers(min_value=2000, max_value=2100))
def test_holidays_are_weekdays_in_year(year):
    from uncorrelated_trading.calendar import nyse_early_closes, nyse_holidays

    hols = nyse_holidays(year)
    assert all(h.weekday() < 5 and h.year == year for h in hols)
    early = nyse_early_closes(year)
    assert not (early & hols)
    assert all(e.weekday() < 5 for e in early)
    n = len(CAL.trading_days(date(year, 1, 1), date(year, 12, 31)))
    assert 245 <= n <= 254
