"""Twelve Data ``time_series`` adapter: one GET per symbol under a token bucket and daily budget.

Twelve Data returns every numeric field as a string and reports most failures as HTTP 200 with
``{"status": "error", "code": ..., "message": ...}``. This adapter maps those payloads to
:class:`~uncorrelated_trading.errors.DataSourceError` (``RateLimitError`` for code 429), casts
values to ``float``/``int`` and returns :class:`~uncorrelated_trading.models.Bar` objects in
ascending date order. The API key travels only in the ``Authorization`` header.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar
from uncorrelated_trading.ratelimit import DailyBudget, TokenBucket

_PROVIDER = "twelvedata"
_URL = "https://api.twelvedata.com/time_series"
_MAX_OUTPUTSIZE = 5000


class TwelveDataSource:
    """Daily bars from Twelve Data, split-adjusted, one request (one credit) per symbol."""

    name = "twelvedata"

    def __init__(
        self,
        api_key: str,
        bucket: TokenBucket,
        budget: DailyBudget,
        session: requests.Session | None = None,
        timeout_s: float = 30.0,
        tz: str = "America/New_York",
    ) -> None:
        """Create the source.

        Parameters
        ----------
        api_key : str
            Twelve Data API key (sent as ``Authorization: apikey <key>``).
        bucket : TokenBucket
            Per-minute limiter; ``acquire()`` is called before every request.
        budget : DailyBudget
            Daily credit budget; ``spend(1)`` is called before every request.
        session : requests.Session or None
            HTTP session (injected in tests); a new one is created when ``None``.
        timeout_s : float
            Per-request timeout in seconds.
        tz : str
            Exchange time zone used to turn ``end_utc`` into the exchange-local end date.
        """
        self._api_key = api_key
        self._bucket = bucket
        self._budget = budget
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s
        self._tz = ZoneInfo(tz)

    def __repr__(self) -> str:
        """Return a representation that never contains the API key."""
        return f"TwelveDataSource(api_key='***', timeout_s={self._timeout_s})"

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        """Fetch daily bars for each symbol between ``start`` and the local date of ``end_utc``.

        Parameters
        ----------
        symbols : Sequence[str]
            Ticker symbols; one request is made per symbol.
        start : date
            First exchange-local day (inclusive).
        end_utc : datetime
            Tz-aware end instant; its exchange-local date is the last day requested.

        Returns
        -------
        list[Bar]
            Bars ordered by symbol (in request order) then ascending day.

        Raises
        ------
        RateLimitError
            On HTTP 429, a ``code: 429`` payload, or an exhausted daily budget.
        DataSourceError
            On any other provider or transport failure.
        """
        end_day = end_utc.astimezone(self._tz).date()
        bars: list[Bar] = []
        for symbol in symbols:
            params = {
                "symbol": symbol,
                "interval": "1day",
                "start_date": start.isoformat(),
                "end_date": end_day.isoformat(),
                "outputsize": str(_MAX_OUTPUTSIZE),
                "format": "JSON",
                "adjust": "splits",
            }
            payload = self._request(params)
            bars.extend(
                b for b in self.parse_time_series(symbol, payload) if start <= b.day <= end_day
            )
        return bars

    def latest_closes(self, symbols: Sequence[str], n: int = 3) -> dict[str, list[Bar]]:
        """Fetch the most recent ``n`` daily bars for each symbol (``outputsize=n``).

        Parameters
        ----------
        symbols : Sequence[str]
            Ticker symbols; one request (one credit) per symbol.
        n : int
            Number of most recent bars to request.

        Returns
        -------
        dict[str, list[Bar]]
            Bars per symbol in ascending day order.

        Raises
        ------
        RateLimitError
            On HTTP 429, a ``code: 429`` payload, or an exhausted daily budget.
        DataSourceError
            On any other provider or transport failure.
        """
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            params = {
                "symbol": symbol,
                "interval": "1day",
                "outputsize": str(n),
                "format": "JSON",
                "adjust": "splits",
            }
            out[symbol] = self.parse_time_series(symbol, self._request(params))
        return out

    @staticmethod
    def parse_time_series(symbol: str, payload: Mapping[str, Any]) -> list[Bar]:
        """Convert a successful ``time_series`` payload into bars.

        Parameters
        ----------
        symbol : str
            Symbol the payload belongs to.
        payload : Mapping[str, Any]
            Decoded JSON with a ``values`` list of string-valued OHLCV records.

        Returns
        -------
        list[Bar]
            Bars in ascending day order (missing ``volume`` becomes 0); empty when the payload
            has no ``values``.
        """
        values = payload.get("values") or []
        bars = [
            Bar(
                symbol=symbol,
                day=date.fromisoformat(str(v["datetime"])[:10]),
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
                volume=int(float(v.get("volume") or 0)),
                source=_PROVIDER,
            )
            for v in values
        ]
        bars.sort(key=lambda b: b.day)
        return bars

    def _request(self, params: Mapping[str, str]) -> Mapping[str, Any]:
        """Acquire a token, spend one credit, GET ``time_series`` and validate the payload."""
        self._bucket.acquire()
        self._budget.spend(1)
        try:
            resp = self._session.get(
                _URL,
                params=dict(params),
                headers={"Authorization": f"apikey {self._api_key}"},
                timeout=self._timeout_s,
            )
        except requests.RequestException as exc:
            raise DataSourceError(
                _PROVIDER, f"transport error: {type(exc).__name__}", retryable=True
            ) from exc
        if resp.status_code == 429:
            raise RateLimitError(_PROVIDER)
        if resp.status_code != 200:
            raise DataSourceError(
                _PROVIDER, f"HTTP {resp.status_code}", retryable=resp.status_code >= 500
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataSourceError(_PROVIDER, "invalid JSON response", retryable=True) from exc
        if not isinstance(payload, Mapping):
            raise DataSourceError(_PROVIDER, "unexpected response shape", retryable=False)
        if payload.get("status") == "error":
            code = int(payload.get("code") or 0)
            message = str(payload.get("message", "unknown error"))
            if code == 429:
                raise RateLimitError(_PROVIDER, message)
            raise DataSourceError(_PROVIDER, f"code {code}: {message}", retryable=code >= 500)
        return payload
