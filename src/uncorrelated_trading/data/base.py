"""Protocol every market-data source implements."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from uncorrelated_trading.models import Bar


class MarketDataSource(Protocol):
    """A provider of daily OHLCV bars."""

    name: str

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        """Return daily bars for ``symbols`` from ``start`` (inclusive) up to ``end_utc``.

        ``end_utc`` comes from :func:`uncorrelated_trading.calendar.data_request_end` so the
        Alpaca SIP 15-minute restriction is respected. Implementations raise
        :class:`uncorrelated_trading.errors.DataSourceError` on failure.
        """
        ...
