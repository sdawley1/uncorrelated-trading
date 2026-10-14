import os
from datetime import UTC, datetime, timedelta

import pytest

from uncorrelated_trading.broker.alpaca_account import AlpacaAccountSource
from uncorrelated_trading.clock import SystemClock
from uncorrelated_trading.data.alpaca_source import AlpacaDataSource
from uncorrelated_trading.ratelimit import TokenBucket

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")),
        reason="ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
    ),
]
TZ = "America/New_York"


def test_live_daily_bars() -> None:
    source = AlpacaDataSource(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        feed="sip",
        adjustment="split",
        symbols_per_request=40,
        bucket=TokenBucket(180, SystemClock()),
        tz=TZ,
    )
    end = datetime.now(UTC) - timedelta(days=1)
    start = (end - timedelta(days=10)).date()
    bars = source.fetch_daily_bars(["XOM", "CVX", "XLE"], start, end)
    for symbol in ("XOM", "CVX", "XLE"):
        assert sum(1 for b in bars if b.symbol == symbol) >= 5


def test_live_account_is_paper() -> None:
    paper = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"
    source = AlpacaAccountSource(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=paper,
        clock=SystemClock(),
        tz=TZ,
    )
    snap = source.account()
    if "ALPACA_PAPER" not in os.environ:
        assert snap.paper is True
    assert snap.equity >= 0.0
    assert isinstance(source.positions(), list)
