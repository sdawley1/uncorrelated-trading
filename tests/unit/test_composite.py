import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from conftest import make_bars
from uncorrelated_trading.data.composite import CompositeDataSource, CompositeFetchResult
from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar
from uncorrelated_trading.ratelimit import DailyBudget

START = date(2026, 9, 1)
END_UTC = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)
AS_OF = date(2026, 10, 13)
SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE"]


class FakeSource:
    def __init__(
        self,
        name: str,
        bars: list[Bar],
        errors: list[Exception] | None = None,
        missing: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.bars = [replace(b, source=name) for b in bars]
        self.errors = list(errors or [])
        self.missing = set(missing)
        self.calls: list[list[str]] = []

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        self.calls.append(list(symbols))
        if self.errors:
            raise self.errors.pop(0)
        end_day = end_utc.date()
        return [
            b
            for b in self.bars
            if b.symbol in symbols and b.symbol not in self.missing and start <= b.day <= end_day
        ]


class FakeLatest(FakeSource):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.latest_calls: list[tuple[list[str], int]] = []

    def latest_closes(self, symbols: Sequence[str], n: int = 3) -> dict[str, list[Bar]]:
        self.latest_calls.append((list(symbols), n))
        return {s: [b for b in self.bars if b.symbol == s][-n:] for s in symbols}


class Usage:
    def __init__(self, used: int = 0) -> None:
        self.used = used

    def used_today(self) -> int:
        return self.used

    def record(self, n: int) -> None:
        self.used += n


def budget(limit: int = 800, used: int = 0) -> DailyBudget:
    u = Usage(used)
    return DailyBudget(limit, u.used_today, u.record)


def make_composite(
    primary: FakeSource,
    fallback: FakeSource | None,
    *,
    fallback_max: int = 20,
    crosscheck_fraction: float = 0.0,
    min_budget: int = 100,
    daily_budget: DailyBudget | None = None,
    sleeps: list[float] | None = None,
) -> CompositeDataSource:
    record = sleeps if sleeps is not None else []
    return CompositeDataSource(
        primary,
        fallback,
        tolerance=0.005,
        fallback_max=fallback_max,
        crosscheck_fraction=crosscheck_fraction,
        min_budget_for_crosscheck=min_budget,
        budget=daily_budget if daily_budget is not None else budget(),
        sleep=record.append,
    )


def retryable() -> DataSourceError:
    return DataSourceError("alpaca", "503", retryable=True)


@pytest.fixture
def bars() -> list[Bar]:
    return make_bars(SYMS, 40)


def test_composite_uses_primary_when_ok(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeSource("twelvedata", bars)
    result = make_composite(primary, fallback).fetch(SYMS, START, END_UTC, AS_OF)
    assert isinstance(result, CompositeFetchResult)
    assert fallback.calls == []
    assert result.quality == ()
    assert result.primary_failures == 0
    assert result.fallback_calls == 0
    assert {b.symbol for b in result.bars} == set(SYMS)
    assert all(b.source == "alpaca" for b in result.bars)


def test_composite_retries_then_falls_back(bars):
    primary = FakeSource("alpaca", bars, errors=[retryable() for _ in range(10)])
    fallback = FakeSource("twelvedata", bars)
    sleeps: list[float] = []
    result = make_composite(primary, fallback, sleeps=sleeps).fetch(SYMS, START, END_UTC, AS_OF)
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(primary.calls) == 4  # initial attempt + 3 retries
    assert fallback.calls == [[s] for s in SYMS]
    assert result.fallback_calls == 5
    assert result.primary_failures == 5
    assert [q.status for q in result.quality] == ["fallback_used"] * 5
    assert {q.symbol for q in result.quality} == set(SYMS)
    assert all(q.source == "twelvedata" and q.bars_available > 0 for q in result.quality)
    assert all(b.source == "twelvedata" for b in result.bars)


def test_composite_retry_then_success_no_fallback(bars):
    primary = FakeSource("alpaca", bars, errors=[retryable()])
    fallback = FakeSource("twelvedata", bars)
    sleeps: list[float] = []
    result = make_composite(primary, fallback, sleeps=sleeps).fetch(SYMS, START, END_UTC, AS_OF)
    assert sleeps == [1.0]
    assert fallback.calls == []
    assert result.primary_failures == 0


def test_composite_non_retryable_goes_straight_to_fallback(bars):
    primary = FakeSource("alpaca", bars, errors=[DataSourceError("alpaca", "401", retryable=False)])
    fallback = FakeSource("twelvedata", bars)
    sleeps: list[float] = []
    result = make_composite(primary, fallback, sleeps=sleeps).fetch(SYMS, START, END_UTC, AS_OF)
    assert sleeps == []
    assert len(primary.calls) == 1
    assert result.fallback_calls == 5


def test_composite_only_missing_symbols_fall_back(bars):
    primary = FakeSource("alpaca", bars, missing=["CCC"])
    fallback = FakeSource("twelvedata", bars)
    result = make_composite(primary, fallback).fetch(SYMS, START, END_UTC, AS_OF)
    assert fallback.calls == [["CCC"]]
    assert result.primary_failures == 1
    assert [(q.symbol, q.status) for q in result.quality] == [("CCC", "fallback_used")]
    sources = {b.symbol: b.source for b in result.bars}
    assert sources["CCC"] == "twelvedata" and sources["AAA"] == "alpaca"


def test_composite_fallback_capped(bars):
    primary = FakeSource("alpaca", bars, missing=SYMS)
    fallback = FakeSource("twelvedata", bars)
    result = make_composite(primary, fallback, fallback_max=2).fetch(SYMS, START, END_UTC, AS_OF)
    assert fallback.calls == [["AAA"], ["BBB"]]
    assert result.fallback_calls == 2
    assert result.primary_failures == 5
    status = {q.symbol: q.status for q in result.quality}
    assert status == {
        "AAA": "fallback_used",
        "BBB": "fallback_used",
        "CCC": "dropped_missing",
        "DDD": "dropped_missing",
        "EEE": "dropped_missing",
    }
    assert {b.symbol for b in result.bars} == {"AAA", "BBB"}


def test_composite_fallback_error_and_empty_drop_symbol(bars):
    primary = FakeSource("alpaca", bars, missing=["AAA", "BBB"])
    fallback = FakeSource(
        "twelvedata",
        bars,
        errors=[DataSourceError("twelvedata", "bad symbol", retryable=False)],
        missing=["BBB"],
    )
    result = make_composite(primary, fallback).fetch(SYMS, START, END_UTC, AS_OF)
    assert result.fallback_calls == 2
    status = {q.symbol: q.status for q in result.quality}
    assert status == {"AAA": "dropped_missing", "BBB": "dropped_missing"}


def test_composite_fallback_stops_on_rate_limit(bars):
    primary = FakeSource("alpaca", bars, missing=SYMS)
    fallback = FakeSource("twelvedata", bars, errors=[RateLimitError("twelvedata")])
    result = make_composite(primary, fallback).fetch(SYMS, START, END_UTC, AS_OF)
    assert result.fallback_calls == 1
    assert all(q.status == "dropped_missing" for q in result.quality)
    assert len(result.quality) == 5


def test_composite_without_fallback_drops_missing(bars):
    primary = FakeSource("alpaca", bars, missing=["EEE"])
    result = make_composite(primary, None).fetch(SYMS, START, END_UTC, AS_OF)
    assert [(q.symbol, q.status) for q in result.quality] == [("EEE", "dropped_missing")]
    assert result.fallback_calls == 0
    assert result.primary_failures == 1


def _shift_last_close(bars: list[Bar], symbol: str, factor: float) -> list[Bar]:
    last = max(b.day for b in bars if b.symbol == symbol)
    return [
        replace(b, close=b.close * factor) if b.symbol == symbol and b.day == last else b
        for b in bars
    ]


def test_composite_crosscheck_flags_mismatch(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeLatest("twelvedata", _shift_last_close(bars, "BBB", 1.01))
    composite = make_composite(primary, fallback, crosscheck_fraction=1.0)
    result = composite.fetch(SYMS, START, END_UTC, AS_OF)
    assert sorted(s for s, _ in [(c[0][0], c[1]) for c in fallback.latest_calls]) == SYMS
    assert all(n == 3 for _, n in fallback.latest_calls)
    assert fallback.calls == []
    mism = [q for q in result.quality if q.status == "close_mismatch"]
    assert [q.symbol for q in mism] == ["BBB"]
    p = max((b for b in bars if b.symbol == "BBB"), key=lambda b: b.day).close
    assert f"primary={p:.6g}" in mism[0].detail
    assert f"fallback={p * 1.01:.6g}" in mism[0].detail
    assert "rel_diff=0.01" in mism[0].detail
    kept = max((b for b in result.bars if b.symbol == "BBB"), key=lambda b: b.day)
    assert kept.close == p and kept.source == "alpaca"


def test_crosscheck_falls_back_to_fetch_daily_bars(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeSource("twelvedata", _shift_last_close(bars, "AAA", 0.98))
    result = make_composite(primary, fallback, crosscheck_fraction=1.0).fetch(
        SYMS, START, END_UTC, AS_OF
    )
    assert sorted(c[0] for c in fallback.calls) == SYMS
    assert [q.symbol for q in result.quality if q.status == "close_mismatch"] == ["AAA"]


def test_crosscheck_within_tolerance_is_silent(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeLatest("twelvedata", _shift_last_close(bars, "BBB", 1.001))
    result = make_composite(primary, fallback, crosscheck_fraction=1.0).fetch(
        SYMS, START, END_UTC, AS_OF
    )
    assert result.quality == ()


def test_crosscheck_sample_rotates_daily():
    symbols = [f"S{i:02d}" for i in range(23)]
    fraction = 0.2
    k = math.ceil(len(symbols) * fraction)
    day0 = date(2026, 10, 13)
    samples = [
        CompositeDataSource.crosscheck_sample(symbols, fraction, day0 + timedelta(days=i))
        for i in range(math.ceil(1 / fraction))
    ]
    assert all(len(s) == k for s in samples)
    assert samples[0] != samples[1]
    assert set(samples[0]).isdisjoint(samples[1])
    assert set().union(*samples) == set(symbols)
    # deterministic and order-independent
    assert (
        CompositeDataSource.crosscheck_sample(list(reversed(symbols)), fraction, day0) == samples[0]
    )


def test_crosscheck_sample_edge_cases():
    assert CompositeDataSource.crosscheck_sample([], 0.2, AS_OF) == []
    assert CompositeDataSource.crosscheck_sample(["A", "B"], 0.0, AS_OF) == []
    assert sorted(CompositeDataSource.crosscheck_sample(["B", "A"], 1.0, AS_OF)) == ["A", "B"]


def test_crosscheck_skipped_when_budget_low(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeLatest("twelvedata", _shift_last_close(bars, "BBB", 1.05))
    composite = make_composite(
        primary,
        fallback,
        crosscheck_fraction=1.0,
        min_budget=100,
        daily_budget=budget(limit=800, used=750),
    )
    result = composite.fetch(SYMS, START, END_UTC, AS_OF)
    assert fallback.latest_calls == []
    assert fallback.calls == []
    assert result.quality == ()


def test_crosscheck_skipped_without_fallback(bars):
    primary = FakeSource("alpaca", bars)
    result = make_composite(primary, None, crosscheck_fraction=1.0).fetch(
        SYMS, START, END_UTC, AS_OF
    )
    assert result.quality == ()


def test_fetch_daily_bars_returns_bars(bars):
    primary = FakeSource("alpaca", bars)
    composite = make_composite(primary, None)
    assert composite.name == "composite"
    out = composite.fetch_daily_bars(SYMS, START, END_UTC)
    assert {b.symbol for b in out} == set(SYMS)


def test_fallback_budget_caps_below_fallback_max(bars):
    primary = FakeSource("alpaca", bars, missing=SYMS)
    fallback = FakeSource("twelvedata", bars)
    composite = make_composite(primary, fallback, fallback_max=20)
    result = composite.fetch(SYMS, START, END_UTC, AS_OF, fallback_budget=1)
    assert fallback.calls == [["AAA"]]
    assert result.fallback_calls == 1
    drops = [q for q in result.quality if q.status == "dropped_missing"]
    assert len(drops) == 4 and "cap" in drops[0].detail
    none_left = composite.fetch(SYMS, START, END_UTC, AS_OF, fallback_budget=0)
    assert none_left.fallback_calls == 0 and len(fallback.calls) == 1


def test_crosscheck_can_be_disabled_per_call(bars):
    primary = FakeSource("alpaca", bars)
    fallback = FakeLatest("twelvedata", _shift_last_close(bars, "BBB", 1.01))
    composite = make_composite(primary, fallback, crosscheck_fraction=1.0)
    result = composite.fetch(SYMS, START, END_UTC, AS_OF, crosscheck=False)
    assert fallback.latest_calls == [] and fallback.calls == []
    assert result.quality == ()
