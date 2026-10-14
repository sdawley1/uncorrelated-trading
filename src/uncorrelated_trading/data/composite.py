"""Composite market-data source: primary with retries, per-symbol fallback, rotating cross-check.

Behaviour (spec A7):

1. The primary (Alpaca) is asked for the whole batch; ``retryable`` errors are retried with
   exponential backoff (default 1 s, 2 s, 4 s after the initial attempt).
2. Symbols for which the primary returned no bars (or every symbol, if the batch failed) are
   fetched from the fallback (Twelve Data) one symbol per request, capped at ``fallback_max``
   (or the caller's remaining per-run ``fallback_budget``);
   they are tagged ``fallback_used``. Symbols still missing are tagged ``dropped_missing``.
3. A deterministic, daily-rotating sample of ``ceil(N * fraction)`` symbols is cross-checked
   against the fallback's latest closes while the daily budget allows; a relative difference
   above ``tolerance`` yields ``close_mismatch``. The primary's values are always kept.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from uncorrelated_trading.data.base import MarketDataSource
from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar, SymbolQuality
from uncorrelated_trading.ratelimit import DailyBudget

_LOG = logging.getLogger(__name__)
_TZ = ZoneInfo("America/New_York")
_CROSSCHECK_CLOSES = 3
_CROSSCHECK_LOOKBACK_DAYS = 7


@dataclass(frozen=True, slots=True)
class CompositeFetchResult:
    """Outcome of :meth:`CompositeDataSource.fetch`."""

    bars: list[Bar]
    quality: tuple[SymbolQuality, ...]  # fallback_used / close_mismatch / dropped_missing entries
    primary_failures: int
    fallback_calls: int


class CompositeDataSource:
    """Primary source with retries, capped per-symbol fallback and rotating cross-validation."""

    name = "composite"

    def __init__(
        self,
        primary: MarketDataSource,
        fallback: MarketDataSource | None,
        tolerance: float,
        fallback_max: int,
        crosscheck_fraction: float,
        min_budget_for_crosscheck: int,
        budget: DailyBudget | None,
        retries: int = 3,
        backoff_s: Sequence[float] = (1.0, 2.0, 4.0),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the composite.

        Parameters
        ----------
        primary : MarketDataSource
            Main provider, queried for the whole batch.
        fallback : MarketDataSource or None
            Secondary provider used per symbol and for cross-checks; ``None`` disables both.
        tolerance : float
            Relative close difference above which a cross-check reports ``close_mismatch``.
        fallback_max : int
            Maximum number of fallback requests per fetch.
        crosscheck_fraction : float
            Fraction of the universe cross-checked per run (``ceil(N * fraction)`` symbols).
        min_budget_for_crosscheck : int
            Cross-checks run only while the fallback's remaining daily budget is at least this.
        budget : DailyBudget or None
            The fallback's daily budget (``None``: no budget gate on cross-checks).
        retries : int
            Number of retries of the primary after the initial attempt on retryable errors.
        backoff_s : Sequence[float]
            Sleep before each retry; the last value is reused if there are more retries.
        sleep : Callable[[float], None]
            Sleep function (injected in tests).
        """
        self._primary = primary
        self._fallback = fallback
        self._tolerance = tolerance
        self._fallback_max = fallback_max
        self._fraction = crosscheck_fraction
        self._min_budget = min_budget_for_crosscheck
        self._budget = budget
        self._retries = retries
        self._backoff_s = tuple(backoff_s)
        self._sleep = sleep

    def fetch(
        self,
        symbols: Sequence[str],
        start: date,
        end_utc: datetime,
        as_of_day: date,
        *,
        fallback_budget: int | None = None,
        crosscheck: bool = True,
    ) -> CompositeFetchResult:
        """Fetch bars with fallback and cross-check, reporting per-symbol quality.

        Parameters
        ----------
        symbols : Sequence[str]
            Requested symbols (duplicates ignored).
        start : date
            First exchange-local day (inclusive).
        end_utc : datetime
            Tz-aware request end (see :func:`uncorrelated_trading.calendar.data_request_end`).
        as_of_day : date
            As-of trading day; keys the cross-check rotation.
        fallback_budget : int or None
            Fallback requests still allowed in the caller's run; the cap for this call is
            ``min(fallback_max, fallback_budget)``. None applies ``fallback_max`` alone.
        crosscheck : bool
            Run the rotating cross-check (False for retry and re-fetch calls of a run).

        Returns
        -------
        CompositeFetchResult
            Bars (primary values preferred), quality rows, primary failure and fallback counts.

        Notes
        -----
        Provider failures (:class:`~uncorrelated_trading.errors.DataSourceError`) never
        propagate; they become quality rows. Other exceptions propagate unchanged.
        """
        wanted = list(dict.fromkeys(symbols))
        bars = self._fetch_primary(wanted, start, end_utc)
        have = {b.symbol for b in bars}
        missing = [s for s in wanted if s not in have]
        quality: list[SymbolQuality] = []
        fallback_calls = 0
        if missing:
            cap = self._fallback_max
            if fallback_budget is not None:
                cap = max(0, min(cap, fallback_budget))
            fb_bars, fb_quality, fallback_calls = self._fetch_fallback(missing, start, end_utc, cap)
            bars.extend(fb_bars)
            quality.extend(fb_quality)
        primary_symbols = [s for s in wanted if s in have]
        if crosscheck:
            quality.extend(self._crosscheck(wanted, primary_symbols, bars, end_utc, as_of_day))
        return CompositeFetchResult(
            bars=bars,
            quality=tuple(quality),
            primary_failures=len(missing),
            fallback_calls=fallback_calls,
        )

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        """Protocol-compatible fetch: ``fetch(...)`` keyed on the New York date of ``end_utc``.

        Parameters
        ----------
        symbols : Sequence[str]
            Requested symbols.
        start : date
            First day (inclusive).
        end_utc : datetime
            Tz-aware request end.

        Returns
        -------
        list[Bar]
            The bars of :meth:`fetch` (quality rows are discarded).
        """
        as_of_day = end_utc.astimezone(_TZ).date()
        return self.fetch(symbols, start, end_utc, as_of_day).bars

    @staticmethod
    def crosscheck_sample(symbols: Sequence[str], fraction: float, as_of_day: date) -> list[str]:
        """Pick the deterministic cross-check sample for ``as_of_day``.

        The sorted universe is split into consecutive blocks of ``k = ceil(n * fraction)``
        symbols; the block start is ``(as_of_day.toordinal() * k) % n`` (wrapping), so
        consecutive calendar days take adjacent blocks and every symbol is covered within
        ``ceil(1 / fraction)`` days.

        Parameters
        ----------
        symbols : Sequence[str]
            Universe (order and duplicates do not matter).
        fraction : float
            Share of the universe to sample per day, in [0, 1].
        as_of_day : date
            Day keying the rotation.

        Returns
        -------
        list[str]
            Up to ``n`` distinct symbols; empty when the universe or fraction is empty.
        """
        ordered = sorted(set(symbols))
        n = len(ordered)
        if n == 0 or fraction <= 0:
            return []
        k = min(n, math.ceil(n * fraction))
        offset = (as_of_day.toordinal() * k) % n
        return [ordered[(offset + i) % n] for i in range(k)]

    # ------------------------------------------------------------------ internals

    def _fetch_primary(self, symbols: list[str], start: date, end_utc: datetime) -> list[Bar]:
        """Fetch the whole batch from the primary, retrying retryable errors with backoff."""
        if not symbols:
            return []
        for attempt in range(self._retries + 1):
            try:
                return list(self._primary.fetch_daily_bars(symbols, start, end_utc))
            except DataSourceError as exc:
                if not exc.retryable or attempt == self._retries:
                    _LOG.warning(
                        "primary %s failed for %d symbols after %d attempt(s): %s",
                        self._primary.name,
                        len(symbols),
                        attempt + 1,
                        exc,
                    )
                    return []
                delay = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
                _LOG.info(
                    "primary %s retryable error (%s); retry in %.1fs",
                    self._primary.name,
                    exc,
                    delay,
                )
                self._sleep(delay)
        return []

    def _fetch_fallback(
        self, missing: list[str], start: date, end_utc: datetime, cap: int
    ) -> tuple[list[Bar], list[SymbolQuality], int]:
        """Fetch ``missing`` symbols one per request from the fallback, up to ``cap``."""
        bars: list[Bar] = []
        quality: list[SymbolQuality] = []
        calls = 0
        stopped = ""
        for symbol in missing:
            if self._fallback is None:
                quality.append(_dropped(symbol, "no data from primary; no fallback configured"))
                continue
            if stopped:
                quality.append(_dropped(symbol, stopped))
                continue
            if calls >= cap:
                quality.append(
                    _dropped(
                        symbol,
                        f"no data from primary; fallback cap {self._fallback_max} per run reached",
                    )
                )
                continue
            calls += 1
            try:
                got = self._fallback.fetch_daily_bars([symbol], start, end_utc)
            except RateLimitError as exc:
                stopped = f"no data from primary; fallback rate limited: {exc}"
                quality.append(_dropped(symbol, stopped))
                continue
            except DataSourceError as exc:
                quality.append(_dropped(symbol, f"no data from primary; fallback failed: {exc}"))
                continue
            got = [b for b in got if b.symbol == symbol]
            if not got:
                quality.append(_dropped(symbol, "no data from primary or fallback"))
                continue
            bars.extend(got)
            quality.append(
                SymbolQuality(
                    symbol=symbol,
                    status="fallback_used",
                    detail=f"primary {self._primary.name} returned no data",
                    bars_available=len(got),
                    source=self._fallback.name,
                )
            )
        return bars, quality, calls

    def _crosscheck(
        self,
        universe: list[str],
        primary_symbols: list[str],
        bars: list[Bar],
        end_utc: datetime,
        as_of_day: date,
    ) -> list[SymbolQuality]:
        """Compare the latest primary close with the fallback's for the rotating sample."""
        fallback = self._fallback
        if fallback is None:
            return []
        eligible = set(primary_symbols)
        sample = [
            s for s in self.crosscheck_sample(universe, self._fraction, as_of_day) if s in eligible
        ]
        primary_name = self._primary.name
        closes: dict[str, dict[date, float]] = {}
        for b in bars:
            if b.symbol in eligible and b.source == primary_name:
                closes.setdefault(b.symbol, {})[b.day] = b.close
        out: list[SymbolQuality] = []
        for symbol in sample:
            if self._budget is not None and self._budget.remaining() < self._min_budget:
                _LOG.info("cross-check skipped: fallback budget below %d", self._min_budget)
                break
            try:
                fb_bars = self._fallback_recent(fallback, symbol, end_utc)
            except RateLimitError as exc:
                _LOG.info("cross-check stopped: %s", exc)
                break
            except DataSourceError as exc:
                _LOG.warning("cross-check of %s failed: %s", symbol, exc)
                continue
            mine = closes.get(symbol, {})
            theirs = {b.day: b.close for b in fb_bars if b.symbol == symbol}
            common = set(mine) & set(theirs)
            if not common:
                _LOG.info("cross-check of %s: no common day", symbol)
                continue
            day = max(common)
            p, f = mine[day], theirs[day]
            rel = _rel_diff(p, f)
            if rel > self._tolerance:
                out.append(
                    SymbolQuality(
                        symbol=symbol,
                        status="close_mismatch",
                        detail=f"primary={p:.6g} fallback={f:.6g} rel_diff={rel:.4g}",
                        bars_available=len(mine),
                        source=primary_name,
                    )
                )
        return out

    @staticmethod
    def _fallback_recent(fallback: MarketDataSource, symbol: str, end_utc: datetime) -> list[Bar]:
        """Return the fallback's most recent bars for ``symbol`` (``latest_closes`` if offered)."""
        latest = getattr(fallback, "latest_closes", None)
        if callable(latest):
            result: dict[str, list[Bar]] = latest([symbol], n=_CROSSCHECK_CLOSES)
            return list(result.get(symbol, []))
        end_day = end_utc.astimezone(_TZ).date()
        start = end_day - timedelta(days=_CROSSCHECK_LOOKBACK_DAYS)
        return fallback.fetch_daily_bars([symbol], start, end_utc)


def _dropped(symbol: str, detail: str) -> SymbolQuality:
    """Build a ``dropped_missing`` quality row."""
    return SymbolQuality(
        symbol=symbol, status="dropped_missing", detail=detail, bars_available=0, source=""
    )


def _rel_diff(primary: float, fallback: float) -> float:
    """Relative difference of ``fallback`` from ``primary`` (inf if primary is 0 and differs)."""
    if primary == fallback:
        return 0.0
    if primary == 0:
        return math.inf
    return abs(fallback - primary) / abs(primary)
