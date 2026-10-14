"""Live Twelve Data smoke test (one API credit); skipped without TWELVEDATA_API_KEY."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from uncorrelated_trading.clock import SystemClock
from uncorrelated_trading.data.twelvedata_source import TwelveDataSource
from uncorrelated_trading.ratelimit import DailyBudget, TokenBucket

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.environ.get("TWELVEDATA_API_KEY"), reason="TWELVEDATA_API_KEY not set")
def test_twelvedata_live_xom():
    spent: list[int] = []
    source = TwelveDataSource(
        os.environ["TWELVEDATA_API_KEY"],
        TokenBucket(8, SystemClock()),
        DailyBudget(800, lambda: sum(spent), spent.append),
    )
    end_utc = datetime.now(UTC)
    bars = source.fetch_daily_bars(["XOM"], (end_utc - timedelta(days=10)).date(), end_utc)
    assert spent == [1]
    assert 1 <= len(bars) <= 10
    assert all(b.symbol == "XOM" and b.source == "twelvedata" and b.close > 0 for b in bars)
    assert [b.day for b in bars] == sorted(b.day for b in bars)
