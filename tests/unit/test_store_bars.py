from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from conftest import make_bars
from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.models import Bar, TradingDay
from uncorrelated_trading.store.bars import BarStore
from uncorrelated_trading.store.db import connect, migrate


@pytest.fixture
def store(tmp_path: Path) -> BarStore:
    conn = connect(tmp_path / "db.sqlite3")
    migrate(conn)
    return BarStore(conn, clock=FakeClock(datetime(2026, 10, 13, 22, 0, tzinfo=UTC)))


def _count(store: BarStore) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0])


def test_upsert_twice_same_row_count(store: BarStore):
    bars = make_bars(["AAA", "BBB"], 30)
    assert store.upsert(bars) == 60
    assert _count(store) == 60
    store.upsert(bars)
    assert _count(store) == 60


def test_upsert_replaces_values(store: BarStore):
    b = Bar("AAA", date(2026, 10, 13), 1.0, 2.0, 0.5, 1.5, 10, "alpaca")
    store.upsert([b])
    b2 = Bar("AAA", date(2026, 10, 13), 1.0, 2.0, 0.5, 1.7, 11, "twelvedata")
    store.upsert([b2])
    assert store.load(["AAA"], date(2026, 10, 1), date(2026, 10, 31)) == [b2]


def test_last_day_none_for_unknown_symbol(store: BarStore):
    assert store.last_day("ZZZ") is None


def test_last_day_returns_latest(store: BarStore):
    store.upsert(make_bars(["AAA"], 10))
    assert store.last_day("AAA") == date(2026, 10, 13)


def test_load_inclusive_range_ascending(store: BarStore):
    bars = make_bars(["AAA", "BBB", "CCC"], 20)
    store.upsert(reversed(bars))
    start, end = date(2026, 10, 1), date(2026, 10, 8)
    got = store.load(["BBB", "AAA"], start, end)
    expected = sorted(
        (b for b in bars if b.symbol in {"AAA", "BBB"} and start <= b.day <= end),
        key=lambda b: (b.symbol, b.day),
    )
    assert got == expected
    assert got[0].day == start
    assert got[-1].day == end
    days_aaa = [b.day for b in got if b.symbol == "AAA"]
    assert days_aaa == sorted(days_aaa)


def test_load_empty_symbols(store: BarStore):
    store.upsert(make_bars(["AAA"], 5))
    assert store.load([], date(2026, 1, 1), date(2026, 12, 31)) == []


def test_days_available(store: BarStore):
    store.upsert(make_bars(["AAA"], 20))
    assert store.days_available("AAA", date(2026, 10, 1), date(2026, 10, 8)) == 6
    assert store.days_available("ZZZ", date(2026, 10, 1), date(2026, 10, 8)) == 0


def test_trading_days_round_trip(store: BarStore):
    days = [
        TradingDay(
            date(2026, 10, d),
            datetime(2026, 10, d, 13, 30, tzinfo=UTC),
            datetime(2026, 10, d, 20, 0, tzinfo=UTC),
        )
        for d in (12, 13, 14, 15)
    ]
    store.upsert_trading_days(days)
    store.upsert_trading_days(days)
    assert store.trading_days(date(2026, 10, 13), date(2026, 10, 14)) == days[1:3]
    assert store.trading_days(date(2026, 11, 1), date(2026, 11, 2)) == []


def test_calendar_refreshed_meta(store: BarStore):
    assert store.calendar_last_refreshed() is None
    when = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    store.set_calendar_refreshed(when)
    assert store.calendar_last_refreshed() == when
    later = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    store.set_calendar_refreshed(later)
    got = store.calendar_last_refreshed()
    assert got == later
    assert got is not None and got.tzinfo is not None


def test_set_calendar_refreshed_rejects_naive(store: BarStore):
    with pytest.raises(ValueError, match="tz-aware"):
        store.set_calendar_refreshed(datetime(2026, 10, 1))  # noqa: DTZ001
