"""Alpaca daily-bar source built on ``alpaca-py``'s ``StockHistoricalDataClient``.

The SDK is imported lazily so the package imports (and tests run) without it. Daily bars are
stamped by Alpaca at midnight exchange-local time expressed in UTC (for example
``2026-10-13T04:00:00Z``); :meth:`AlpacaDataSource.parse_bars` converts them to the
exchange-local trading date. SDK and transport failures are mapped onto
:class:`~uncorrelated_trading.errors.DataSourceError` by :func:`map_alpaca_error`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar

PROVIDER = "alpaca"
_REQUEST_LIMIT = 10000

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "t"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "v"),
}


class _Bucket(Protocol):
    """Anything with a blocking ``acquire()`` (``ratelimit.TokenBucket``)."""

    def acquire(self) -> None:
        """Block until one request may be made."""
        ...


# ----------------------------------------------------------------------------------------------
# Helpers shared with broker.alpaca_account
# ----------------------------------------------------------------------------------------------


def get_field(obj: Any, name: str) -> Any:
    """Read ``name`` from an SDK model (attribute) or a JSON dict (key).

    Parameters
    ----------
    obj : Any
        SDK model object or mapping.
    name : str
        Field name.

    Returns
    -------
    Any
        The value, or None when absent.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def parse_timestamp(value: Any) -> datetime:
    """Convert an ISO 8601 string or tz-aware datetime to a tz-aware datetime.

    Parameters
    ----------
    value : Any
        ``datetime`` or string such as ``"2026-10-13T04:00:00Z"``.

    Returns
    -------
    datetime
        Tz-aware instant (naive values are taken as UTC, as Alpaca documents).

    Raises
    ------
    ValueError
        If ``value`` is neither a datetime nor a parseable string.
    """
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        ts = datetime.fromisoformat(text)
    else:
        msg = f"unsupported timestamp {value!r}"
        raise ValueError(msg)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts


def _api_error_text(exc: Exception) -> str:
    """Return Alpaca's ``message`` from an ``APIError`` body, else ``str(exc)``."""
    raw = str(exc)
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(body, Mapping) and isinstance(body.get("message"), str):
        return str(body["message"])
    return raw


def map_alpaca_error(exc: Exception) -> DataSourceError:
    """Map an SDK or transport exception onto the application error hierarchy.

    Parameters
    ----------
    exc : Exception
        Exception raised by an ``alpaca-py`` client call.

    Returns
    -------
    DataSourceError
        ``RateLimitError`` for HTTP 429; non-retryable for 401/403 (a "subscription" message
        additionally explains the SIP 15-minute rule) and other 4xx; retryable for 5xx,
        missing status codes and connection errors (``OSError``, which includes
        ``requests.RequestException``). Anything else is returned non-retryable.
    """
    if isinstance(exc, DataSourceError):
        return exc
    if isinstance(exc, OSError):
        return DataSourceError(PROVIDER, f"connection error: {exc}", retryable=True)
    from alpaca.common.exceptions import APIError

    if not isinstance(exc, APIError):
        return DataSourceError(
            PROVIDER, f"unexpected error: {type(exc).__name__}: {exc}", retryable=False
        )
    status = exc.status_code
    text = _api_error_text(exc)
    if status == 429:
        return RateLimitError(PROVIDER, f"HTTP 429: {text}")
    if status in (401, 403):
        if "subscription" in text.lower():
            return DataSourceError(
                PROVIDER,
                f'HTTP {status}: Alpaca says "{text}". Recent SIP data needs a paid plan; move '
                "the request end earlier (more than 15 minutes before now) or use feed 'iex'.",
                retryable=False,
            )
        return DataSourceError(
            PROVIDER, f"HTTP {status}: {text} (check API keys and plan)", retryable=False
        )
    if status is None or status >= 500:
        return DataSourceError(PROVIDER, f"HTTP {status}: {text}", retryable=True)
    return DataSourceError(PROVIDER, f"HTTP {status}: {text}", retryable=False)


# ----------------------------------------------------------------------------------------------
# Data source
# ----------------------------------------------------------------------------------------------


def _bar_field(raw: Any, name: str) -> Any:
    """Return a bar field by long or short (``t``, ``o``, ...) name."""
    for key in _FIELD_ALIASES[name]:
        value = get_field(raw, key)
        if value is not None:
            return value
    msg = f"bar is missing field {name!r}"
    raise KeyError(msg)


class AlpacaDataSource:
    """Daily OHLCV bars from Alpaca's historical stock data API.

    Parameters
    ----------
    api_key, secret_key : str
        Alpaca credentials (paper keys suffice).
    feed : str
        ``DataFeed`` value: ``"sip"``, ``"iex"`` or ``"delayed_sip"``.
    adjustment : str
        ``Adjustment`` value: ``"raw"``, ``"split"``, ``"dividend"`` or ``"all"``.
    symbols_per_request : int
        Maximum symbols per multi-symbol request (>= 1).
    bucket : TokenBucket
        Rate limiter; ``acquire()`` is called before every request.
    tz : str
        IANA exchange time zone used to convert bar timestamps to trading dates.
    client : Any, optional
        Injected ``StockHistoricalDataClient`` (tests); built lazily when None.
    """

    name = PROVIDER

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        feed: str,
        adjustment: str,
        symbols_per_request: int,
        bucket: _Bucket,
        tz: str,
        client: Any | None = None,
    ) -> None:
        """Store configuration; no network access or SDK import happens here."""
        if symbols_per_request < 1:
            msg = f"symbols_per_request must be >= 1, got {symbols_per_request}"
            raise ValueError(msg)
        self._api_key = api_key
        self._secret_key = secret_key
        self._feed = feed
        self._adjustment = adjustment
        self._chunk = symbols_per_request
        self._bucket = bucket
        self._tz = tz
        self._client = client

    def __repr__(self) -> str:
        """Return a representation that never includes credentials."""
        return f"AlpacaDataSource(feed={self._feed!r}, adjustment={self._adjustment!r})"

    def _get_client(self) -> Any:
        """Return the injected client or lazily build a ``StockHistoricalDataClient``."""
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        """Fetch daily bars in chunks of at most ``symbols_per_request`` symbols.

        Parameters
        ----------
        symbols : Sequence[str]
            Tickers to fetch.
        start : date
            First exchange-local date (inclusive); sent as local midnight.
        end_utc : datetime
            Tz-aware request end (see ``calendar.data_request_end``).

        Returns
        -------
        list[Bar]
            Bars for every chunk, in response order. The SDK follows ``next_page_token``
            internally, so each chunk is a single client call.

        Raises
        ------
        DataSourceError
            Mapped by :func:`map_alpaca_error`; ``RateLimitError`` on HTTP 429.
        """
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start_local = datetime.combine(start, time(0), ZoneInfo(self._tz))
        out: list[Bar] = []
        for i in range(0, len(symbols), self._chunk):
            chunk = list(symbols[i : i + self._chunk])
            request = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start_local,
                end=end_utc,
                limit=_REQUEST_LIMIT,
                adjustment=Adjustment(self._adjustment),
                feed=DataFeed(self._feed),
            )
            self._bucket.acquire()
            try:
                response = self._get_client().get_stock_bars(request)
            except Exception as exc:
                raise map_alpaca_error(exc) from exc
            data = getattr(response, "data", response)
            out.extend(self.parse_bars(data, self._tz))
        return out

    @staticmethod
    def parse_bars(raw: Mapping[str, Any], tz: str) -> list[Bar]:
        """Convert an Alpaca bars payload to :class:`Bar` objects.

        Parameters
        ----------
        raw : Mapping[str, Any]
            Either ``BarSet.data`` (``{symbol: [bar, ...]}``) or a raw JSON page
            ``{"bars": {symbol: [dict, ...]}, "next_page_token": ...}``. Bars may be SDK
            objects or dicts with short (``t, o, h, l, c, v``) or long field names.
        tz : str
            IANA exchange time zone; each timestamp becomes ``ts.astimezone(tz).date()``.

        Returns
        -------
        list[Bar]
            Bars with ``source="alpaca"`` and integer (rounded) volume.

        Raises
        ------
        DataSourceError
            Non-retryable, when a bar is malformed.
        """
        zone = ZoneInfo(tz)
        bars_by_symbol = raw["bars"] if "bars" in raw and "next_page_token" in raw else raw
        out: list[Bar] = []
        for symbol, items in (bars_by_symbol or {}).items():
            for item in items or []:
                if item is None:
                    continue
                try:
                    ts = parse_timestamp(_bar_field(item, "timestamp"))
                    out.append(
                        Bar(
                            symbol=str(symbol),
                            day=ts.astimezone(zone).date(),
                            open=float(_bar_field(item, "open")),
                            high=float(_bar_field(item, "high")),
                            low=float(_bar_field(item, "low")),
                            close=float(_bar_field(item, "close")),
                            volume=int(round(float(_bar_field(item, "volume")))),
                            source=PROVIDER,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataSourceError(
                        PROVIDER, f"malformed bar for {symbol}: {exc}", retryable=False
                    ) from exc
        return out
