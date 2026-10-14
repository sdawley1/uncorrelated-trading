"""Read-only Alpaca account adapter: balances, positions and the exchange calendar.

Wraps ``alpaca-py``'s ``TradingClient`` (imported lazily). There is no order code here or
anywhere in the project. Alpaca returns money fields as strings; they are cast to ``float``
at this boundary. Calendar sessions arrive as local wall times (``"09:30"``, ``"16:00"``)
and are converted to UTC instants in the exchange time zone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.data.alpaca_source import PROVIDER, get_field, map_alpaca_error
from uncorrelated_trading.errors import DataSourceError
from uncorrelated_trading.models import AccountSnapshot, PositionSnapshot, TradingDay


def _required_float(obj: Any, name: str) -> float:
    """Return ``float(obj.name)``; raise ``DataSourceError`` when absent or invalid."""
    value = get_field(obj, name)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DataSourceError(
            PROVIDER, f"malformed field {name!r}: {value!r}", retryable=False
        ) from exc


def _optional_float(obj: Any, name: str) -> float:
    """Return ``float(obj.name)`` or 0.0 when Alpaca omits the optional field."""
    if get_field(obj, name) is None:
        return 0.0
    return _required_float(obj, name)


def _as_date(value: Any) -> date:
    """Convert a ``date``/``datetime`` or ``YYYY-MM-DD`` string to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _session_instant(day: date, value: Any, zone: ZoneInfo) -> datetime:
    """Build a UTC instant from a local wall time (``"09:30"``, naive or aware datetime)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        local = value.replace(tzinfo=zone)
    else:
        local = datetime.combine(day, time.fromisoformat(str(value)), zone)
    return local.astimezone(UTC)


class AlpacaAccountSource:
    """Alpaca ``TradingClient`` adapter implementing ``broker.base.AccountSource``.

    Parameters
    ----------
    api_key, secret_key : str
        Alpaca credentials.
    paper : bool
        True for the paper endpoint (``ALPACA_PAPER`` default); False only switches account
        reads to the live endpoint.
    clock : Clock
        Supplies ``taken_at`` for account snapshots.
    tz : str
        IANA exchange time zone for calendar sessions.
    client : Any, optional
        Injected ``TradingClient`` (tests); built lazily when None.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool,
        clock: Clock,
        tz: str,
        client: Any | None = None,
    ) -> None:
        """Store configuration; no network access or SDK import happens here."""
        self.paper = paper
        self._api_key = api_key
        self._secret_key = secret_key
        self._clock = clock
        self._tz = tz
        self._client = client

    def __repr__(self) -> str:
        """Return a representation that never includes credentials."""
        return f"AlpacaAccountSource(paper={self.paper!r})"

    def _get_client(self) -> Any:
        """Return the injected client or lazily build a ``TradingClient``."""
        if self._client is None:
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(self._api_key, self._secret_key, paper=self.paper)
        return self._client

    def account(self) -> AccountSnapshot:
        """Read current balances.

        Returns
        -------
        AccountSnapshot
            Balances with ``taken_at = clock.now()``.

        Raises
        ------
        DataSourceError
            On any API or transport failure.
        """
        try:
            raw = self._get_client().get_account()
        except Exception as exc:
            raise map_alpaca_error(exc) from exc
        return self.parse_account(raw, taken_at=self._clock.now(), paper=self.paper)

    def positions(self) -> list[PositionSnapshot]:
        """Read open positions.

        Returns
        -------
        list[PositionSnapshot]
            One entry per open position, in API order.

        Raises
        ------
        DataSourceError
            On any API or transport failure.
        """
        try:
            raw = self._get_client().get_all_positions()
        except Exception as exc:
            raise map_alpaca_error(exc) from exc
        return self.parse_positions(raw)

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Read exchange sessions from Alpaca's calendar endpoint.

        Parameters
        ----------
        start, end : date
            Inclusive date range.

        Returns
        -------
        list[TradingDay]
            Sessions with UTC open and close instants.

        Raises
        ------
        DataSourceError
            On any API or transport failure.
        """
        from alpaca.trading.requests import GetCalendarRequest

        try:
            raw = self._get_client().get_calendar(GetCalendarRequest(start=start, end=end))
        except Exception as exc:
            raise map_alpaca_error(exc) from exc
        return self.parse_calendar(raw, self._tz)

    @staticmethod
    def parse_account(raw: Mapping[str, Any], taken_at: datetime, paper: bool) -> AccountSnapshot:
        """Convert an Alpaca account (``TradeAccount`` or JSON dict) to a snapshot.

        Parameters
        ----------
        raw : Mapping[str, Any]
            Account payload; string-typed money fields are cast to ``float``.
        taken_at : datetime
            Tz-aware instant of the read.
        paper : bool
            Whether the paper endpoint was used.

        Returns
        -------
        AccountSnapshot
            Parsed balances.

        Raises
        ------
        DataSourceError
            Non-retryable, when a required field is missing or not numeric.
        """
        currency = get_field(raw, "currency")
        return AccountSnapshot(
            taken_at=taken_at,
            cash=_required_float(raw, "cash"),
            equity=_required_float(raw, "equity"),
            buying_power=_optional_float(raw, "buying_power"),
            portfolio_value=_optional_float(raw, "portfolio_value"),
            last_equity=_optional_float(raw, "last_equity"),
            currency=str(currency) if currency is not None else "USD",
            paper=paper,
        )

    @staticmethod
    def parse_positions(raw: Sequence[Mapping[str, Any]]) -> list[PositionSnapshot]:
        """Convert Alpaca positions (``Position`` models or JSON dicts) to snapshots.

        Parameters
        ----------
        raw : Sequence[Mapping[str, Any]]
            Positions payload; optional fields Alpaca omits become 0.0.

        Returns
        -------
        list[PositionSnapshot]
            Parsed positions, in input order.

        Raises
        ------
        DataSourceError
            Non-retryable, when a required field is missing or not numeric.
        """
        out: list[PositionSnapshot] = []
        for item in raw:
            out.append(
                PositionSnapshot(
                    symbol=str(get_field(item, "symbol")),
                    qty=_required_float(item, "qty"),
                    avg_entry_price=_required_float(item, "avg_entry_price"),
                    current_price=_optional_float(item, "current_price"),
                    market_value=_optional_float(item, "market_value"),
                    cost_basis=_optional_float(item, "cost_basis"),
                    unrealized_pl=_optional_float(item, "unrealized_pl"),
                    unrealized_plpc=_optional_float(item, "unrealized_plpc"),
                    change_today=_optional_float(item, "change_today"),
                )
            )
        return out

    @staticmethod
    def parse_calendar(raw: Sequence[Mapping[str, Any]], tz: str) -> list[TradingDay]:
        """Convert Alpaca calendar rows to :class:`TradingDay` objects.

        Parameters
        ----------
        raw : Sequence[Mapping[str, Any]]
            Rows with ``date`` and local ``open``/``close`` wall times, either as JSON strings
            (``"09:30"``) or as the SDK's naive local datetimes.
        tz : str
            IANA exchange time zone the wall times are expressed in.

        Returns
        -------
        list[TradingDay]
            Sessions sorted by day, with UTC ``open_utc`` and ``close_utc``.

        Raises
        ------
        DataSourceError
            Non-retryable, when a row is malformed.
        """
        zone = ZoneInfo(tz)
        out: list[TradingDay] = []
        for row in raw:
            try:
                day = _as_date(get_field(row, "date"))
                out.append(
                    TradingDay(
                        day=day,
                        open_utc=_session_instant(day, get_field(row, "open"), zone),
                        close_utc=_session_instant(day, get_field(row, "close"), zone),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DataSourceError(
                    PROVIDER, f"malformed calendar row {row!r}: {exc}", retryable=False
                ) from exc
        return sorted(out, key=lambda d: d.day)
