from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from uncorrelated_trading.calendar import (
    CachedCalendar,
    StaticCalendar,
    data_request_end,
    nyse_early_closes,
    nyse_holidays,
    previous_trading_day,
    resolve_as_of,
    snap_as_of,
)
from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.models import TradingDay

NY = ZoneInfo("America/New_York")
CAL = StaticCalendar()


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


# ---------------------------------------------------------------- resolve_as_of (brief)


def test_midday_on_trading_day_is_intraday_previous_day():
    a = resolve_as_of(at(2026, 10, 13, 12, 0), CAL, "America/New_York", 20)
    assert a.mode == "intraday" and a.day == date(2026, 10, 12)


def test_pre_open_uses_previous_day():
    a = resolve_as_of(at(2026, 10, 13, 8, 0), CAL, "America/New_York", 20)
    assert a.mode == "pre_open" and a.day == date(2026, 10, 12)


def test_between_close_and_settle_is_provisional_today():
    a = resolve_as_of(at(2026, 10, 13, 16, 5), CAL, "America/New_York", 20)
    assert a.mode == "post_close_settling" and a.day == date(2026, 10, 13) and a.provisional


def test_after_settle_is_post_close_today():
    a = resolve_as_of(at(2026, 10, 13, 16, 25), CAL, "America/New_York", 20)
    assert a.mode == "post_close" and a.day == date(2026, 10, 13) and not a.provisional


def test_early_close_day_after_thanksgiving():
    # 2026-11-27 closes 13:00 ET
    a = resolve_as_of(at(2026, 11, 27, 13, 30), CAL, "America/New_York", 20)
    assert a.mode == "post_close" and a.day == date(2026, 11, 27)


def test_static_calendar_known_holidays_2026():
    days = {d.day for d in CAL.trading_days(date(2026, 1, 1), date(2026, 12, 31))}
    for holiday in [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ]:
        assert holiday not in days
    assert date(2026, 10, 13) in days


def test_saturday_uses_friday():
    a = resolve_as_of(at(2026, 10, 17, 9, 0), CAL, "America/New_York", 20)
    assert a.mode == "non_trading_day" and a.day == date(2026, 10, 16)


def test_holiday_uses_prior_trading_day():
    a = resolve_as_of(at(2026, 11, 26, 9, 0), CAL, "America/New_York", 20)
    assert a.day == date(2026, 11, 25)


def test_request_end_never_within_sip_guard():
    now = at(2026, 10, 13, 16, 25)
    a = resolve_as_of(now, CAL, "America/New_York", 20)
    end = data_request_end(a, "America/New_York", now)
    assert end <= now - __import__("datetime").timedelta(minutes=16)


# ---------------------------------------------------------------- resolve_as_of (extra)


def test_exact_open_is_intraday_and_exact_close_is_settling():
    assert resolve_as_of(at(2026, 10, 13, 9, 30), CAL, "America/New_York", 20).mode == "intraday"
    a = resolve_as_of(at(2026, 10, 13, 16, 0), CAL, "America/New_York", 20)
    assert a.mode == "post_close_settling"
    b = resolve_as_of(at(2026, 10, 13, 16, 20), CAL, "America/New_York", 20)
    assert b.mode == "post_close"


def test_early_close_settling_window():
    a = resolve_as_of(at(2026, 11, 27, 13, 5), CAL, "America/New_York", 20)
    assert a.mode == "post_close_settling" and a.day == date(2026, 11, 27) and a.provisional
    b = resolve_as_of(at(2026, 11, 27, 12, 59), CAL, "America/New_York", 20)
    assert b.mode == "intraday" and b.day == date(2026, 11, 25)


def test_monday_pre_open_uses_friday():
    a = resolve_as_of(at(2026, 10, 19, 8, 0), CAL, "America/New_York", 20)
    assert a.mode == "pre_open" and a.day == date(2026, 10, 16)


def test_as_of_fields_populated():
    now = at(2026, 10, 13, 12, 0)
    a = resolve_as_of(now, CAL, "America/New_York", 20, calendar_source="alpaca")
    assert a.now_utc == now and a.calendar_source == "alpaca" and not a.provisional
    assert "excluded" in a.note


def test_resolve_rejects_naive_datetime():
    with pytest.raises(ValueError):
        resolve_as_of(datetime(2026, 10, 13, 12, 0), CAL, "America/New_York", 20)  # noqa: DTZ001


def test_late_evening_utc_rollover_uses_local_date():
    # 23:30 ET on Tue 2026-10-13 is 03:30 UTC Wed; local date must stay Tuesday.
    a = resolve_as_of(at(2026, 10, 13, 23, 30), CAL, "America/New_York", 20)
    assert a.mode == "post_close" and a.day == date(2026, 10, 13)


# ---------------------------------------------------------------- StaticCalendar / holidays


def test_holidays_2025():
    assert nyse_holidays(2025) == frozenset(
        [
            date(2025, 1, 1),
            date(2025, 1, 20),
            date(2025, 2, 17),
            date(2025, 4, 18),
            date(2025, 5, 26),
            date(2025, 6, 19),
            date(2025, 7, 4),
            date(2025, 9, 1),
            date(2025, 11, 27),
            date(2025, 12, 25),
        ]
    )


def test_holidays_2026_exact():
    assert nyse_holidays(2026) == frozenset(
        [
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 4, 3),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 11, 26),
            date(2026, 12, 25),
        ]
    )


def test_holidays_2027_observed_rules():
    assert nyse_holidays(2027) == frozenset(
        [
            date(2027, 1, 1),
            date(2027, 1, 18),
            date(2027, 2, 15),
            date(2027, 3, 26),
            date(2027, 5, 31),
            date(2027, 6, 18),
            date(2027, 7, 5),
            date(2027, 9, 6),
            date(2027, 11, 25),
            date(2027, 12, 24),
        ]
    )


def test_saturday_new_year_not_observed_on_friday():
    # 2022-01-01 was a Saturday; NYSE stayed open Friday 2021-12-31.
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2021, 12, 31) not in nyse_holidays(2022)
    assert all(h.year == 2022 for h in nyse_holidays(2022))


def test_sunday_new_year_observed_monday():
    # 2023-01-01 was a Sunday.
    assert date(2023, 1, 2) in nyse_holidays(2023)


def test_early_closes():
    assert nyse_early_closes(2025) == frozenset(
        [date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)]
    )
    # 2026-07-03 is the observed Independence Day holiday, so not an early close.
    assert nyse_early_closes(2026) == frozenset([date(2026, 11, 27), date(2026, 12, 24)])
    # 2027-12-24 is the observed Christmas holiday; 2027-07-03 is a Saturday.
    assert nyse_early_closes(2027) == frozenset([date(2027, 11, 26)])


def test_static_sessions_in_utc():
    (d,) = CAL.trading_days(date(2026, 10, 13), date(2026, 10, 13))
    assert d.open_utc == datetime(2026, 10, 13, 13, 30, tzinfo=UTC)
    assert d.close_utc == datetime(2026, 10, 13, 20, 0, tzinfo=UTC)
    (w,) = CAL.trading_days(date(2026, 1, 13), date(2026, 1, 13))  # EST
    assert w.open_utc == datetime(2026, 1, 13, 14, 30, tzinfo=UTC)
    (e,) = CAL.trading_days(date(2026, 11, 27), date(2026, 11, 27))
    assert e.close_utc == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_static_trading_days_sorted_inclusive_and_empty():
    days = CAL.trading_days(date(2026, 10, 12), date(2026, 10, 18))
    assert [d.day for d in days] == [date(2026, 10, d) for d in range(12, 17)]
    assert CAL.trading_days(date(2026, 10, 18), date(2026, 10, 12)) == []
    assert len(CAL.trading_days(date(2026, 1, 1), date(2026, 12, 31))) == 251


# ---------------------------------------------------------------- previous / snap / request end


def test_previous_trading_day():
    assert previous_trading_day(date(2026, 10, 13), CAL) == date(2026, 10, 12)
    assert previous_trading_day(date(2026, 10, 12), CAL) == date(2026, 10, 9)
    assert previous_trading_day(date(2026, 11, 27), CAL) == date(2026, 11, 25)
    assert previous_trading_day(date(2026, 1, 2), CAL) == date(2025, 12, 31)


def test_snap_as_of_trading_day_kept():
    now = at(2026, 10, 14, 12, 0)
    a = snap_as_of(date(2026, 10, 13), CAL, now)
    assert a.day == date(2026, 10, 13) and a.now_utc == now and not a.provisional
    assert "override" in a.note


def test_snap_as_of_non_trading_day_snaps_back():
    a = snap_as_of(date(2026, 10, 17), CAL, at(2026, 10, 20, 12, 0))
    assert a.day == date(2026, 10, 16) and a.mode == "non_trading_day"
    assert "2026-10-17" in a.note


def test_snap_as_of_future_rejected():
    with pytest.raises(ValueError):
        snap_as_of(date(2026, 10, 20), CAL, at(2026, 10, 14, 12, 0))


def test_snap_as_of_future_uses_exchange_date():
    # 21:00 ET on 2026-10-14 is already 2026-10-15 in UTC; the 15th is still the future.
    with pytest.raises(ValueError, match="future"):
        snap_as_of(date(2026, 10, 15), CAL, at(2026, 10, 14, 21, 0), "America/New_York", 20)
    a = snap_as_of(date(2026, 10, 14), CAL, at(2026, 10, 14, 21, 0), "America/New_York", 20)
    assert a.day == date(2026, 10, 14) and a.mode == "post_close"


@pytest.mark.parametrize(
    ("hh", "mm", "mode", "day", "provisional"),
    [
        (8, 0, "pre_open", date(2026, 10, 13), False),
        (11, 0, "intraday", date(2026, 10, 13), False),
        (16, 5, "post_close_settling", date(2026, 10, 14), True),
        (17, 30, "post_close", date(2026, 10, 14), False),
    ],
)
def test_snap_as_of_today_resolves_like_now(hh, mm, mode, day, provisional):
    now = at(2026, 10, 14, hh, mm)
    a = snap_as_of(date(2026, 10, 14), CAL, now, "America/New_York", 20)
    assert (a.mode, a.day, a.provisional) == (mode, day, provisional)
    assert a.note.startswith("--as-of override 2026-10-14 is today; ")


def test_data_request_end_uses_day_end_when_earlier():
    now = at(2026, 10, 14, 12, 0)
    a = resolve_as_of(now, CAL, "America/New_York", 20)
    assert a.day == date(2026, 10, 13)
    end = data_request_end(a, "America/New_York", now)
    assert end == datetime(2026, 10, 13, 23, 59, 59, tzinfo=NY).astimezone(UTC)
    assert end.tzinfo is not None


# ---------------------------------------------------------------- CachedCalendar


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[date, TradingDay] = {}
        self.refreshed: datetime | None = None

    def upsert_trading_days(self, days):
        for d in days:
            self.rows[d.day] = d

    def trading_days(self, start, end):
        return [self.rows[k] for k in sorted(self.rows) if start <= k <= end]

    def calendar_last_refreshed(self):
        return self.refreshed

    def set_calendar_refreshed(self, when):
        self.refreshed = when


class FakeRemote:
    """Alpaca stand-in: static calendar minus one ad hoc closure (2026-10-14)."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[date, date]] = []
        self.fail = fail

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise ConnectionError("alpaca down")
        return [d for d in CAL.trading_days(start, end) if d.day != date(2026, 10, 14)]


NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)


def test_cached_calendar_refreshes_once_and_serves_remote():
    store, remote = FakeStore(), FakeRemote()
    cal = CachedCalendar(store, remote, FakeClock(NOW), "America/New_York")
    days = [d.day for d in cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))]
    assert date(2026, 10, 14) not in days and date(2026, 10, 15) in days
    assert cal.source == "alpaca"
    assert remote.calls == [
        (date(2026, 10, 15) - timedelta(days=400), date(2026, 10, 15) + timedelta(days=60))
    ]
    assert store.refreshed == NOW
    cal.trading_days(date(2026, 9, 1), date(2026, 10, 16))
    assert len(remote.calls) == 1


def test_cached_calendar_uses_fresh_cache_without_remote():
    store, remote = FakeStore(), FakeRemote()
    CachedCalendar(store, remote, FakeClock(NOW), "America/New_York").trading_days(
        date(2026, 10, 1), date(2026, 10, 2)
    )
    remote2 = FakeRemote(fail=True)
    clock = FakeClock(NOW + timedelta(days=10))
    cal = CachedCalendar(store, remote2, clock, "America/New_York")
    days = cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    assert remote2.calls == [] and cal.source == "alpaca"
    assert date(2026, 10, 14) not in [d.day for d in days]


def test_cached_calendar_stale_cache_triggers_refresh():
    store = FakeStore()
    CachedCalendar(store, FakeRemote(), FakeClock(NOW), "America/New_York").trading_days(
        date(2026, 10, 1), date(2026, 10, 2)
    )
    remote2 = FakeRemote()
    cal = CachedCalendar(store, remote2, FakeClock(NOW + timedelta(days=31)), "America/New_York")
    cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    assert len(remote2.calls) == 1 and store.refreshed == NOW + timedelta(days=31)


def test_cached_calendar_remote_failure_falls_back_to_static():
    store, remote = FakeStore(), FakeRemote(fail=True)
    cal = CachedCalendar(store, remote, FakeClock(NOW), "America/New_York")
    days = cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    assert [d.day for d in days] == [
        d.day for d in CAL.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    ]
    assert cal.source == "static" and len(remote.calls) == 1
    assert store.refreshed is None


def test_cached_calendar_without_remote_is_static():
    cal = CachedCalendar(FakeStore(), None, FakeClock(NOW), "America/New_York")
    assert len(cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))) == 5
    assert cal.source == "static"


def test_cached_calendar_stale_cache_covering_range_survives_remote_failure():
    store = FakeStore()
    CachedCalendar(store, FakeRemote(), FakeClock(NOW), "America/New_York").trading_days(
        date(2026, 10, 1), date(2026, 10, 2)
    )
    remote = FakeRemote(fail=True)
    cal = CachedCalendar(store, remote, FakeClock(NOW + timedelta(days=40)), "America/New_York")
    days = cal.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    assert cal.source == "alpaca" and date(2026, 10, 14) not in [d.day for d in days]
    cal.trading_days(date(2026, 10, 5), date(2026, 10, 9))
    assert len(remote.calls) == 1


def test_cached_calendar_uncovered_range_falls_back_after_one_attempt():
    store, remote = FakeStore(), FakeRemote()
    cal = CachedCalendar(store, remote, FakeClock(NOW), "America/New_York")
    days = cal.trading_days(date(2030, 1, 7), date(2030, 1, 11))
    assert len(days) == 5 and cal.source == "static"
    cal.trading_days(date(2030, 1, 7), date(2030, 1, 11))
    assert len(remote.calls) == 1


def test_resolve_as_of_with_cached_calendar():
    store = FakeStore()
    cal = CachedCalendar(store, FakeRemote(), FakeClock(NOW), "America/New_York")
    # 2026-10-14 is closed in the fake Alpaca calendar. ``source`` is only meaningful after
    # a query, so callers resolve first and re-stamp the source.
    a = resolve_as_of(at(2026, 10, 14, 12, 0), cal, "America/New_York", 20)
    assert a.mode == "non_trading_day" and a.day == date(2026, 10, 13)
    assert cal.source == "alpaca"
