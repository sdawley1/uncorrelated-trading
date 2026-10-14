"""Persistence of daily bars and the cached trading calendar."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from uncorrelated_trading.clock import Clock, SystemClock
from uncorrelated_trading.errors import StoreError
from uncorrelated_trading.models import Bar, TradingDay

_CALENDAR_REFRESHED_KEY = "calendar_last_refreshed"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_utc(when: datetime) -> str:
    """Return ``when`` as an ISO 8601 UTC string; reject naive datetimes."""
    if when.tzinfo is None:
        msg = "datetime must be tz-aware"
        raise ValueError(msg)
    return when.astimezone(UTC).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# BarStore
# ---------------------------------------------------------------------------


class BarStore:
    """Read and write daily bars, calendar days and calendar refresh metadata.

    Parameters
    ----------
    conn : sqlite3.Connection
        Connection on a migrated database (see :func:`store.db.migrate`).
    clock : Clock, optional
        Source of ``fetched_at`` timestamps; defaults to the system clock.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()

    def upsert(self, bars: Iterable[Bar]) -> int:
        """Insert or replace bars keyed on ``(symbol, day)``.

        Parameters
        ----------
        bars : Iterable[Bar]
            Bars to write; later duplicates of a key win.

        Returns
        -------
        int
            Number of rows written (one per input bar).
        """
        fetched_at = _iso_utc(self._clock.now())
        rows = [
            (
                b.symbol,
                b.day.isoformat(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.source,
                fetched_at,
            )
            for b in bars
        ]
        try:
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO bars (symbol, day, open, high, low, close, volume,"
                    " source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            msg = f"bar upsert failed: {exc}"
            raise StoreError(msg) from exc
        return len(rows)

    def last_day(self, symbol: str) -> date | None:
        """Return the latest stored day for ``symbol``, or None if it has no bars.

        Parameters
        ----------
        symbol : str
            Ticker.

        Returns
        -------
        date | None
            Latest bar date.
        """
        row = self._one("SELECT MAX(day) FROM bars WHERE symbol = ?", (symbol,))
        return None if row is None or row[0] is None else date.fromisoformat(row[0])

    def load(self, symbols: Sequence[str], start: date, end: date) -> list[Bar]:
        """Load bars for ``symbols`` with ``start <= day <= end``.

        Parameters
        ----------
        symbols : Sequence[str]
            Tickers to load; unknown tickers are ignored.
        start, end : date
            Inclusive date range.

        Returns
        -------
        list[Bar]
            Bars sorted ascending by ``(symbol, day)``.
        """
        if not symbols:
            return []
        placeholders = ",".join("?" * len(symbols))
        sql = (
            "SELECT symbol, day, open, high, low, close, volume, source FROM bars"
            f" WHERE symbol IN ({placeholders}) AND day >= ? AND day <= ?"
            " ORDER BY symbol, day"
        )
        params = (*symbols, start.isoformat(), end.isoformat())
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = f"bar load failed: {exc}"
            raise StoreError(msg) from exc
        return [
            Bar(
                symbol=r[0],
                day=date.fromisoformat(r[1]),
                open=float(r[2]),
                high=float(r[3]),
                low=float(r[4]),
                close=float(r[5]),
                volume=int(r[6]),
                source=r[7],
            )
            for r in rows
        ]

    def days_available(self, symbol: str, start: date, end: date) -> int:
        """Count stored bars for ``symbol`` with ``start <= day <= end``.

        Parameters
        ----------
        symbol : str
            Ticker.
        start, end : date
            Inclusive date range.

        Returns
        -------
        int
            Number of bars.
        """
        row = self._one(
            "SELECT COUNT(*) FROM bars WHERE symbol = ? AND day >= ? AND day <= ?",
            (symbol, start.isoformat(), end.isoformat()),
        )
        return 0 if row is None else int(row[0])

    def upsert_trading_days(self, days: Iterable[TradingDay]) -> None:
        """Insert or replace cached calendar sessions.

        Parameters
        ----------
        days : Iterable[TradingDay]
            Sessions to cache (open/close instants must be tz-aware).
        """
        rows = [(d.day.isoformat(), _iso_utc(d.open_utc), _iso_utc(d.close_utc)) for d in days]
        try:
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO calendar_days (day, open_utc, close_utc)"
                    " VALUES (?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            msg = f"calendar upsert failed: {exc}"
            raise StoreError(msg) from exc

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return cached sessions with ``start <= day <= end``, ascending.

        Parameters
        ----------
        start, end : date
            Inclusive date range.

        Returns
        -------
        list[TradingDay]
            Cached sessions (empty if none are cached in the range).
        """
        try:
            rows = self.conn.execute(
                "SELECT day, open_utc, close_utc FROM calendar_days"
                " WHERE day >= ? AND day <= ? ORDER BY day",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        except sqlite3.Error as exc:
            msg = f"calendar load failed: {exc}"
            raise StoreError(msg) from exc
        return [
            TradingDay(
                day=date.fromisoformat(r[0]),
                open_utc=datetime.fromisoformat(r[1]),
                close_utc=datetime.fromisoformat(r[2]),
            )
            for r in rows
        ]

    def calendar_last_refreshed(self) -> datetime | None:
        """Return when the calendar cache was last refreshed, or None if never.

        Returns
        -------
        datetime | None
            Tz-aware UTC instant.
        """
        row = self._one("SELECT value FROM meta WHERE key = ?", (_CALENDAR_REFRESHED_KEY,))
        return None if row is None else datetime.fromisoformat(row[0])

    def set_calendar_refreshed(self, when: datetime) -> None:
        """Record the calendar cache refresh instant.

        Parameters
        ----------
        when : datetime
            Tz-aware instant.

        Raises
        ------
        ValueError
            If ``when`` is naive.
        """
        value = _iso_utc(when)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (_CALENDAR_REFRESHED_KEY, value),
                )
        except sqlite3.Error as exc:
            msg = f"meta write failed: {exc}"
            raise StoreError(msg) from exc

    def _one(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        """Execute a read query and return its first row, wrapping SQLite errors."""
        try:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            msg = f"query failed: {exc}"
            raise StoreError(msg) from exc
        return row
