"""Protocol for read-only brokerage account access (no order placement exists)."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from uncorrelated_trading.models import AccountSnapshot, PositionSnapshot, TradingDay


class AccountSource(Protocol):
    """Read account balances, open positions and the exchange calendar."""

    paper: bool

    def account(self) -> AccountSnapshot:
        """Return current balances; raises ``DataSourceError`` when unavailable."""
        ...

    def positions(self) -> list[PositionSnapshot]:
        """Return open positions; raises ``DataSourceError`` when unavailable."""
        ...

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return exchange sessions in the inclusive date range."""
        ...
