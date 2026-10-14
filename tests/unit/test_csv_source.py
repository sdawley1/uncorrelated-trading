from datetime import UTC, date, datetime
from pathlib import Path

from conftest import make_bars
from uncorrelated_trading.data.csv_source import CsvDataSource

CSV = Path(__file__).resolve().parents[1] / "fixtures" / "csv" / "bars_small.csv"


def test_csv_source_filters_range():
    source = CsvDataSource(CSV)
    assert source.name == "csv"
    bars = source.fetch_daily_bars(
        ["S03", "S07", "NOPE"], date(2026, 10, 1), datetime(2026, 10, 12, 21, 0, tzinfo=UTC)
    )
    assert {b.symbol for b in bars} == {"S03", "S07"}
    days = sorted({b.day for b in bars})
    assert days[0] == date(2026, 10, 1)
    assert days[-1] == date(2026, 10, 12)
    assert all(b.source == "csv" for b in bars)
    assert len(bars) == 2 * len(days)


def test_csv_end_uses_exchange_local_date():
    source = CsvDataSource(CSV)
    bars = source.fetch_daily_bars(
        ["S01"], date(2026, 10, 13), datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
    )
    assert [b.day for b in bars] == [date(2026, 10, 13)]


def test_csv_fixture_matches_make_bars():
    syms = [f"S{i:02d}" for i in range(1, 11)]
    expected = make_bars(syms, 300, seed=0)
    got = CsvDataSource(CSV).fetch_daily_bars(
        syms, date(2000, 1, 1), datetime(2026, 10, 13, 21, 0, tzinfo=UTC)
    )
    assert len(got) == 3000
    assert sorted(got, key=lambda b: (b.symbol, b.day)) == sorted(
        expected, key=lambda b: (b.symbol, b.day)
    )
