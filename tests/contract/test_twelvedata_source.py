"""Contract tests for TwelveDataSource against recorded JSON payloads (no network)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.data.twelvedata_source import TwelveDataSource
from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar
from uncorrelated_trading.ratelimit import DailyBudget, TokenBucket

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "twelvedata"
FAKE_KEY = "td_FAKEKEYXXXXXXXXXXXXXXXX"
NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)
END_UTC = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body

    def json(self) -> Any:
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self, url: str, params: dict[str, str], headers: dict[str, str], timeout: float
    ) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Usage:
    def __init__(self) -> None:
        self.used = 0

    def used_today(self) -> int:
        return self.used

    def record(self, n: int) -> None:
        self.used += n


def make_source(
    responses: list[FakeResponse | Exception], limit: int = 800
) -> tuple[TwelveDataSource, FakeSession, Usage]:
    session = FakeSession(responses)
    usage = Usage()
    source = TwelveDataSource(
        FAKE_KEY,
        TokenBucket(8, FakeClock(NOW)),
        DailyBudget(limit, usage.used_today, usage.record),
        session=session,  # type: ignore[arg-type]
    )
    return source, session, usage


def test_parse_time_series_strings_to_floats_and_dates():
    bars = TwelveDataSource.parse_time_series("XOM", json.loads(load("time_series_ok.json")))
    assert [b.day for b in bars] == [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)]
    assert bars[-1] == Bar(
        "XOM", date(2026, 10, 13), 112.5, 113.75, 111.9, 113.2, 15234100, "twelvedata"
    )
    assert isinstance(bars[0].close, float)
    assert bars[0].volume == 0  # missing volume -> 0


def test_parse_time_series_without_values_is_empty():
    assert TwelveDataSource.parse_time_series("XOM", {"status": "ok"}) == []


def test_request_shape():
    source, session, _ = make_source([FakeResponse(200, load("time_series_ok.json"))])
    bars = source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert len(bars) == 3
    call = session.calls[0]
    assert call["url"] == "https://api.twelvedata.com/time_series"
    assert call["params"] == {
        "symbol": "XOM",
        "interval": "1day",
        "start_date": "2026-10-01",
        "end_date": "2026-10-13",
        "outputsize": "5000",
        "format": "JSON",
        "adjust": "splits",
    }
    assert call["headers"] == {"Authorization": f"apikey {FAKE_KEY}"}
    assert call["timeout"] == 30.0


def test_end_date_is_exchange_local():
    source, session, _ = make_source([FakeResponse(200, load("time_series_ok.json"))])
    # 02:00 UTC on 14 Oct is still 13 Oct in New York.
    source.fetch_daily_bars(["XOM"], date(2026, 10, 1), datetime(2026, 10, 14, 2, 0, tzinfo=UTC))
    assert session.calls[0]["params"]["end_date"] == "2026-10-13"


def test_bars_filtered_to_requested_range():
    source, _, _ = make_source([FakeResponse(200, load("time_series_ok.json"))])
    bars = source.fetch_daily_bars(["XOM"], date(2026, 10, 12), END_UTC)
    assert [b.day for b in bars] == [date(2026, 10, 12), date(2026, 10, 13)]


def test_429_raises_rate_limit():
    source, _, _ = make_source([FakeResponse(200, load("time_series_429.json"))])
    with pytest.raises(RateLimitError) as exc:
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert exc.value.provider == "twelvedata"
    assert exc.value.retryable


def test_http_429_raises_rate_limit():
    source, _, _ = make_source([FakeResponse(429, load("time_series_429.json"))])
    with pytest.raises(RateLimitError):
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)


def test_bad_symbol_raises_not_retryable():
    source, _, _ = make_source([FakeResponse(200, load("time_series_bad_symbol.json"))])
    with pytest.raises(DataSourceError) as exc:
        source.fetch_daily_bars(["NOTREAL"], date(2026, 10, 1), END_UTC)
    assert not isinstance(exc.value, RateLimitError)
    assert exc.value.retryable is False
    assert "symbol" in str(exc.value)


def test_server_error_code_is_retryable():
    body = json.dumps({"status": "error", "code": 500, "message": "internal"})
    source, _, _ = make_source([FakeResponse(200, body)])
    with pytest.raises(DataSourceError) as exc:
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert exc.value.retryable is True


def test_http_5xx_is_retryable():
    source, _, _ = make_source([FakeResponse(503, "Service Unavailable")])
    with pytest.raises(DataSourceError) as exc:
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert exc.value.retryable is True


def test_transport_error_is_retryable():
    source, _, _ = make_source([requests.ConnectionError("boom")])
    with pytest.raises(DataSourceError) as exc:
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert exc.value.retryable is True


def test_budget_spent_per_request():
    ok = load("time_series_ok.json")
    source, session, usage = make_source([FakeResponse(200, ok), FakeResponse(200, ok)])
    source.fetch_daily_bars(["XOM", "CVX"], date(2026, 10, 1), END_UTC)
    assert len(session.calls) == 2
    assert [c["params"]["symbol"] for c in session.calls] == ["XOM", "CVX"]
    assert usage.used == 2


def test_budget_exhausted_blocks_request():
    source, session, usage = make_source([FakeResponse(200, load("time_series_ok.json"))], limit=0)
    with pytest.raises(RateLimitError):
        source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert session.calls == []
    assert usage.used == 0


def test_latest_closes_uses_outputsize_n():
    source, session, usage = make_source([FakeResponse(200, load("time_series_ok.json"))])
    out = source.latest_closes(["XOM"], n=3)
    assert [b.close for b in out["XOM"]] == [111.05, 112.4, 113.2]
    params = session.calls[0]["params"]
    assert params["outputsize"] == "3"
    assert "start_date" not in params and "end_date" not in params
    assert usage.used == 1


def test_api_key_not_in_params_or_repr():
    source, session, _ = make_source([FakeResponse(200, load("time_series_ok.json"))])
    source.fetch_daily_bars(["XOM"], date(2026, 10, 1), END_UTC)
    assert FAKE_KEY not in json.dumps(session.calls[0]["params"])
    assert FAKE_KEY not in repr(source)
