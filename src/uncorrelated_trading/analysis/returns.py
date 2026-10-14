"""Close-price matrix construction and daily log returns.

``build_price_matrix`` aligns daily closes of the requested symbols on a common window of
trading days (the union of days observed across those symbols). Symbols with gaps in the
window are dropped, never forward-filled, and reported as :class:`SymbolQuality` records.
``log_returns`` turns the aligned prices into daily log returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import Bar, SymbolQuality

__all__ = ["PriceMatrixResult", "build_price_matrix", "log_returns"]


@dataclass(frozen=True, slots=True)
class PriceMatrixResult:
    """Aligned close prices plus the symbols that could not be aligned.

    Attributes
    ----------
    prices : pandas.DataFrame
        Index ``date`` (ascending ``datetime.date``), columns the kept symbols in request
        order, dtype float64, no missing values.
    dropped : tuple of SymbolQuality
        One record per dropped symbol, status ``dropped_missing`` or
        ``dropped_short_history``.
    """

    prices: pd.DataFrame
    dropped: tuple[SymbolQuality, ...]


def _unique(symbols: Sequence[str]) -> list[str]:
    """Return ``symbols`` without duplicates, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_price_matrix(
    bars: Sequence[Bar], symbols: Sequence[str], end_day: date, n_days: int
) -> PriceMatrixResult:
    """Build the close-price matrix for the last ``n_days + 1`` trading days.

    Parameters
    ----------
    bars : Sequence[Bar]
        Daily bars; bars of symbols not in ``symbols`` or dated after ``end_day`` are ignored.
        If a symbol has several bars on one day, the last one wins.
    symbols : Sequence[str]
        Requested symbols; their order fixes the column order (duplicates are ignored).
    end_day : date
        Last trading day to include.
    n_days : int
        Number of returns the window must support; ``n_days + 1`` price rows are kept.

    Returns
    -------
    PriceMatrixResult
        Prices of the symbols that have a bar on every window day, plus drop records. A
        symbol whose first bar is after the window start is ``dropped_short_history``; any
        other symbol lacking a window day is ``dropped_missing``.

    Raises
    ------
    AnalysisError
        If ``n_days < 1`` or fewer than ``n_days + 1`` distinct trading days are observed.
    """
    if n_days < 1:
        raise AnalysisError(f"n_days must be >= 1, got {n_days}")
    wanted = _unique(symbols)
    wanted_set = set(wanted)
    closes: dict[str, dict[date, float]] = {s: {} for s in wanted}
    sources: dict[str, str] = {}
    first_day: dict[str, date] = {}
    for bar in bars:
        if bar.symbol not in wanted_set or bar.day > end_day:
            continue
        closes[bar.symbol][bar.day] = float(bar.close)
        prev_first = first_day.get(bar.symbol)
        if prev_first is None or bar.day < prev_first:
            first_day[bar.symbol] = bar.day
        sources[bar.symbol] = bar.source

    all_days = sorted({d for per in closes.values() for d in per})
    n_rows = n_days + 1
    if len(all_days) < n_rows:
        raise AnalysisError(
            f"need {n_rows} trading days up to {end_day.isoformat()}, found {len(all_days)}"
        )
    window = all_days[-n_rows:]
    window_start = window[0]

    kept: list[str] = []
    dropped: list[SymbolQuality] = []
    for sym in wanted:
        per = closes[sym]
        missing = [d for d in window if d not in per]
        available = n_rows - len(missing)
        source = sources.get(sym, "")
        first = first_day.get(sym)
        if not missing:
            kept.append(sym)
        elif first is not None and first > window_start:
            dropped.append(
                SymbolQuality(
                    symbol=sym,
                    status="dropped_short_history",
                    detail=(
                        f"first bar {first.isoformat()} is after window start "
                        f"{window_start.isoformat()}; {available} of {n_rows} days available"
                    ),
                    bars_available=available,
                    source=source,
                )
            )
        else:
            dropped.append(
                SymbolQuality(
                    symbol=sym,
                    status="dropped_missing",
                    detail=(
                        f"missing {len(missing)} of {n_rows} window days; "
                        f"first missing {missing[0].isoformat()}"
                    ),
                    bars_available=available,
                    source=source,
                )
            )

    index = pd.Index(window, name="date")
    data = {sym: [closes[sym][d] for d in window] for sym in kept}
    prices = pd.DataFrame(data, index=index, columns=kept, dtype=np.float64)
    return PriceMatrixResult(prices=prices, dropped=tuple(dropped))


def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute daily log returns of a price matrix.

    Parameters
    ----------
    prices : pandas.DataFrame
        Positive prices, index ascending by day.

    Returns
    -------
    pandas.DataFrame
        ``log(p_t) - log(p_{t-1})``; the first (all-NaN) row is removed, so the result has
        one row fewer than ``prices``.
    """
    logged = pd.DataFrame(
        np.log(prices.to_numpy(dtype=np.float64)), index=prices.index, columns=prices.columns
    )
    return logged.diff().dropna(how="all")
