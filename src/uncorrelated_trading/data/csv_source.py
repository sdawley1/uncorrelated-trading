"""Offline market-data source reading daily bars from a CSV file.

The file has the header ``symbol,day,open,high,low,close,volume`` with ``day`` as
``YYYY-MM-DD``. Used for tests, demos and ``--offline`` runs.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from uncorrelated_trading.errors import DataSourceError
from uncorrelated_trading.models import Bar

_TZ = ZoneInfo("America/New_York")


class CsvDataSource:
    """Daily bars from a local CSV file, tagged ``source="csv"``."""

    name = "csv"

    def __init__(self, path: Path) -> None:
        """Create the source.

        Parameters
        ----------
        path : Path
            CSV file with columns ``symbol,day,open,high,low,close,volume``.
        """
        self._path = path

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        """Return bars for ``symbols`` with ``start <= day <=`` the New York date of ``end_utc``.

        Parameters
        ----------
        symbols : Sequence[str]
            Symbols to return; unknown symbols are silently absent.
        start : date
            First day (inclusive).
        end_utc : datetime
            Tz-aware end instant; its America/New_York date is the last day (inclusive).

        Returns
        -------
        list[Bar]
            Matching bars in file order.

        Raises
        ------
        DataSourceError
            If the file cannot be read or a row is malformed (not retryable).
        """
        wanted = set(symbols)
        end_day = end_utc.astimezone(_TZ).date()
        bars: list[Bar] = []
        try:
            with self._path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row["symbol"] not in wanted:
                        continue
                    day = date.fromisoformat(row["day"])
                    if not start <= day <= end_day:
                        continue
                    bars.append(
                        Bar(
                            symbol=row["symbol"],
                            day=day,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(float(row["volume"] or 0)),
                            source=self.name,
                        )
                    )
        except (OSError, KeyError, ValueError) as exc:
            raise DataSourceError(
                self.name, f"cannot read {self._path.name}: {exc}", retryable=False
            ) from exc
        return bars
