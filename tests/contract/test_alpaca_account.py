import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.common.exceptions import APIError

from uncorrelated_trading.broker.alpaca_account import AlpacaAccountSource
from uncorrelated_trading.broker.null_account import NullAccountSource
from uncorrelated_trading.calendar import StaticCalendar
from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import AccountSnapshot, PositionSnapshot, TradingDay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"
TZ = "America/New_York"
NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeTradingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calendar_requests: list[Any] = []
        self._error = error

    def _maybe_raise(self) -> None:
        if self._error is not None:
            raise self._error

    def get_account(self) -> Any:
        self._maybe_raise()
        return load("account.json")

    def get_all_positions(self) -> Any:
        self._maybe_raise()
        return load("positions.json")

    def get_calendar(self, request: Any) -> Any:
        self._maybe_raise()
        self.calendar_requests.append(request)
        return load("calendar.json")


def make_source(client: Any, paper: bool = True) -> AlpacaAccountSource:
    return AlpacaAccountSource(
        api_key="PKXXXXXXXXXXXXXXXXXX",
        secret_key="FAKESECRETXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        paper=paper,
        clock=FakeClock(NOW),
        tz=TZ,
        client=client,
    )


def test_parse_account_casts_strings() -> None:
    snap = AlpacaAccountSource.parse_account(load("account.json"), taken_at=NOW, paper=True)
    assert snap == AccountSnapshot(
        taken_at=NOW,
        cash=100000.25,
        equity=100512.75,
        buying_power=200000.5,
        portfolio_value=100512.75,
        last_equity=100230.1,
        currency="USD",
        paper=True,
    )


def test_parse_account_accepts_sdk_model() -> None:
    from alpaca.trading.models import TradeAccount

    model = TradeAccount(**load("account.json"))
    snap = AlpacaAccountSource.parse_account(model, taken_at=NOW, paper=False)
    assert snap.cash == 100000.25 and snap.currency == "USD" and snap.paper is False


def test_account_uses_clock_and_paper_flag() -> None:
    snap = make_source(FakeTradingClient()).account()
    assert snap.taken_at == NOW
    assert snap.paper is True


def test_parse_positions() -> None:
    positions = AlpacaAccountSource.parse_positions(load("positions.json"))
    assert positions[0] == PositionSnapshot(
        symbol="XOM",
        qty=3.0,
        avg_entry_price=110.5,
        current_price=112.4,
        market_value=337.2,
        cost_basis=331.5,
        unrealized_pl=5.7,
        unrealized_plpc=0.0171945701357466,
        change_today=0.0035714285714286,
    )
    assert positions[1].symbol == "XLE" and positions[1].unrealized_pl == -2.6
    assert make_source(FakeTradingClient()).positions() == positions


def test_parse_positions_accepts_sdk_models_with_missing_optionals() -> None:
    from alpaca.trading.models import Position

    raw = load("positions.json")[0]
    for key in ("current_price", "change_today", "market_value", "unrealized_pl"):
        raw.pop(key)
    (pos,) = AlpacaAccountSource.parse_positions([Position(**raw)])
    assert pos.symbol == "XOM" and pos.qty == 3.0
    assert pos.current_price == 0.0 and pos.change_today == 0.0


def test_parse_calendar_builds_utc_sessions() -> None:
    days = AlpacaAccountSource.parse_calendar(load("calendar.json"), TZ)
    assert days[0] == TradingDay(
        day=date(2026, 10, 7),
        open_utc=datetime(2026, 10, 7, 13, 30, tzinfo=UTC),
        close_utc=datetime(2026, 10, 7, 20, 0, tzinfo=UTC),
    )
    # after DST ends (EST, UTC-5) with a 13:00 early close
    assert days[-1] == TradingDay(
        day=date(2026, 11, 27),
        open_utc=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        close_utc=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
    )
    assert all(d.open_utc.utcoffset() is not None for d in days)


def test_parse_calendar_accepts_sdk_models() -> None:
    from alpaca.trading.models import Calendar

    models = [Calendar(**row) for row in load("calendar.json")]
    assert AlpacaAccountSource.parse_calendar(models, TZ) == AlpacaAccountSource.parse_calendar(
        load("calendar.json"), TZ
    )


def test_trading_days_requests_range() -> None:
    client = FakeTradingClient()
    days = make_source(client).trading_days(date(2026, 10, 7), date(2026, 10, 13))
    (req,) = client.calendar_requests
    assert (req.start, req.end) == (date(2026, 10, 7), date(2026, 10, 13))
    assert len(days) == 6


def test_account_error_mapped() -> None:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=429), request=None)
    client = FakeTradingClient(error=APIError('{"message": "rate limit"}', http_error))
    with pytest.raises(RateLimitError):
        make_source(client).account()
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=403), request=None)
    client = FakeTradingClient(error=APIError('{"message": "forbidden."}', http_error))
    with pytest.raises(DataSourceError) as info:
        make_source(client).positions()
    assert info.value.retryable is False


def test_null_account_raises_offline() -> None:
    source = NullAccountSource(StaticCalendar(TZ))
    assert source.paper is True
    with pytest.raises(DataSourceError) as info:
        source.account()
    assert info.value.retryable is False
    assert "offline" in str(info.value)
    with pytest.raises(DataSourceError):
        source.positions()
    days = source.trading_days(date(2026, 10, 12), date(2026, 10, 16))
    assert [d.day for d in days] == [date(2026, 10, d) for d in range(12, 17)]


def test_parse_calendar_malformed_row_raises() -> None:
    with pytest.raises(DataSourceError) as info:
        AlpacaAccountSource.parse_calendar([{"date": "2026-10-13", "open": "nope"}], TZ)
    assert info.value.retryable is False


def test_parse_account_missing_cash_raises() -> None:
    raw = load("account.json")
    del raw["cash"]
    with pytest.raises(DataSourceError):
        AlpacaAccountSource.parse_account(raw, taken_at=NOW, paper=True)


def test_repr_hides_credentials() -> None:
    assert "PKXXXX" not in repr(make_source(FakeTradingClient()))
