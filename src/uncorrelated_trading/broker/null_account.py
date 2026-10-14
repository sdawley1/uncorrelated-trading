"""Offline account source: no balances or positions, calendar from a local calendar."""

from __future__ import annotations

from datetime import date

from uncorrelated_trading.calendar import TradingCalendar
from uncorrelated_trading.errors import DataSourceError
from uncorrelated_trading.models import AccountSnapshot, PositionSnapshot, TradingDay

_OFFLINE_MESSAGE = "offline mode: no account access"


class NullAccountSource:
    """``AccountSource`` used with ``--offline`` when no Alpaca keys are configured.

    Parameters
    ----------
    calendar : TradingCalendar
        Calendar that serves :meth:`trading_days` (typically ``StaticCalendar``).
    """

    paper = True

    def __init__(self, calendar: TradingCalendar) -> None:
        """Wrap ``calendar``."""
        self._calendar = calendar

    def account(self) -> AccountSnapshot:
        """Raise, because balances are unavailable offline.

        Raises
        ------
        DataSourceError
            Always, with ``retryable=False``.
        """
        raise DataSourceError("alpaca", _OFFLINE_MESSAGE, retryable=False)

    def positions(self) -> list[PositionSnapshot]:
        """Raise, because positions are unavailable offline.

        Raises
        ------
        DataSourceError
            Always, with ``retryable=False``.
        """
        raise DataSourceError("alpaca", _OFFLINE_MESSAGE, retryable=False)

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return sessions in ``[start, end]`` from the wrapped calendar.

        Parameters
        ----------
        start, end : date
            Inclusive date range.

        Returns
        -------
        list[TradingDay]
            Sessions as reported by the wrapped calendar.
        """
        return self._calendar.trading_days(start, end)
