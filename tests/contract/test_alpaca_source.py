import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from alpaca.common.exceptions import APIError

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.data.alpaca_source import AlpacaDataSource
from uncorrelated_trading.errors import DataSourceError, RateLimitError
from uncorrelated_trading.models import Bar
from uncorrelated_trading.ratelimit import TokenBucket

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"
TZ = "America/New_York"
FAKE_KEY = "PKXXXXXXXXXXXXXXXXXX"
FAKE_SECRET = "FAKESECRETXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
END = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def bar_dict(t: str, close: float = 10.0) -> dict[str, Any]:
    return {"t": t, "o": 9.5, "h": 10.5, "l": 9.0, "c": close, "v": 1000.4, "n": 10, "vw": 9.9}


class CountingBucket:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


class FakeClient:
    """Fake StockHistoricalDataClient: returns BarSet-like objects or raises."""

    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None):
        self.requests: list[Any] = []
        self._responses = list(responses or [])
        self._error = error
        self.bucket: CountingBucket | None = None
        self.bucket_calls_at_request: list[int] = []

    def get_stock_bars(self, request: Any) -> Any:
        self.requests.append(request)
        if self.bucket is not None:
            self.bucket_calls_at_request.append(self.bucket.calls)
        if self._error is not None:
            raise self._error
        if self._responses:
            return SimpleNamespace(data=self._responses.pop(0))
        symbols = request.symbol_or_symbols
        return SimpleNamespace(data={s: [bar_dict("2026-10-13T04:00:00Z")] for s in symbols})


def make_source(
    client: FakeClient, symbols_per_request: int = 40, bucket: Any = None
) -> AlpacaDataSource:
    return AlpacaDataSource(
        api_key=FAKE_KEY,
        secret_key=FAKE_SECRET,
        feed="sip",
        adjustment="split",
        symbols_per_request=symbols_per_request,
        bucket=bucket if bucket is not None else CountingBucket(),
        tz=TZ,
        client=client,
    )


def api_error(status: int, body: dict[str, Any]) -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
    return APIError(json.dumps(body), http_error)


def as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def test_parse_bars_converts_midnight_et_timestamp_to_date() -> None:
    raw = {
        "XOM": [bar_dict("2026-10-13T04:00:00Z")],
        "CVX": [bar_dict("2026-03-09T04:00:00Z")],  # day after DST start
        "XLE": [bar_dict("2026-03-06T05:00:00Z")],  # before DST start (EST)
    }
    bars = {b.symbol: b for b in AlpacaDataSource.parse_bars(raw, TZ)}
    assert bars["XOM"].day == date(2026, 10, 13)
    assert bars["CVX"].day == date(2026, 3, 9)
    assert bars["XLE"].day == date(2026, 3, 6)
    assert bars["XOM"] == Bar("XOM", date(2026, 10, 13), 9.5, 10.5, 9.0, 10.0, 1000, "alpaca")


def test_parse_bars_accepts_sdk_bar_objects() -> None:
    from alpaca.data.models.bars import BarSet

    barset = BarSet(load("bars_page1.json")["bars"])
    bars = AlpacaDataSource.parse_bars(barset.data, TZ)
    xom = [b for b in bars if b.symbol == "XOM"]
    assert [b.day for b in xom] == [
        date(2026, 10, 7),
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 12),
        date(2026, 10, 13),
    ]
    assert all(isinstance(b.volume, int) for b in bars)


def test_parse_bars_accepts_long_field_names_and_datetimes() -> None:
    raw = {
        "AAA": [
            SimpleNamespace(
                timestamp=datetime(2026, 10, 13, 4, 0, tzinfo=UTC),
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                volume=99.6,
            )
        ]
    }
    (bar,) = AlpacaDataSource.parse_bars(raw, TZ)
    assert bar == Bar("AAA", date(2026, 10, 13), 1.0, 2.0, 0.5, 1.5, 100, "alpaca")


def test_parse_bars_malformed_raises_not_retryable() -> None:
    with pytest.raises(DataSourceError) as info:
        AlpacaDataSource.parse_bars({"AAA": [{"t": "2026-10-13T04:00:00Z"}]}, TZ)
    assert info.value.retryable is False


def test_fetch_chunks_symbols() -> None:
    client = FakeClient()
    source = make_source(client, symbols_per_request=2)
    bars = source.fetch_daily_bars(["AAA", "BBB", "CCC"], date(2026, 10, 1), END)
    assert [list(r.symbol_or_symbols) for r in client.requests] == [["AAA", "BBB"], ["CCC"]]
    assert sorted(b.symbol for b in bars) == ["AAA", "BBB", "CCC"]


def test_fetch_builds_request() -> None:
    from alpaca.data.enums import Adjustment, DataFeed

    client = FakeClient()
    make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    (req,) = client.requests
    # the SDK normalises aware datetimes to naive UTC
    assert as_utc(req.start) == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    assert as_utc(req.end) == END
    assert req.limit == 10000
    assert req.feed == DataFeed.SIP
    assert req.adjustment == Adjustment.SPLIT
    assert req.timeframe.amount_value == 1
    assert req.timeframe.unit_value.value == "Day"


def test_fetch_follows_next_page_token() -> None:
    page1 = load("bars_page1.json")
    page2 = load("bars_page2.json")
    assert page1["next_page_token"]
    assert page2["next_page_token"] is None
    bars = AlpacaDataSource.parse_bars(page1, TZ) + AlpacaDataSource.parse_bars(page2, TZ)
    counts = {s: sum(1 for b in bars if b.symbol == s) for s in ("XOM", "CVX", "XLE")}
    assert counts == {"XOM": 5, "CVX": 5, "XLE": 5}
    assert len({(b.symbol, b.day) for b in bars}) == 15
    # the SDK merges pages internally: fetch makes exactly one client call per chunk
    merged = {s: page1["bars"].get(s, []) + page2["bars"].get(s, []) for s in counts}
    client = FakeClient(responses=[merged])
    fetched = make_source(client).fetch_daily_bars(["XOM", "CVX", "XLE"], date(2026, 10, 7), END)
    assert len(client.requests) == 1
    assert sorted((b.symbol, b.day) for b in fetched) == sorted((b.symbol, b.day) for b in bars)


def test_fetch_acquires_bucket_per_request() -> None:
    bucket = CountingBucket()
    client = FakeClient()
    client.bucket = bucket
    make_source(client, symbols_per_request=1, bucket=bucket).fetch_daily_bars(
        ["AAA", "BBB", "CCC"], date(2026, 10, 1), END
    )
    assert bucket.calls == 3
    assert client.bucket_calls_at_request == [1, 2, 3]


def test_fetch_with_real_token_bucket() -> None:
    clock = FakeClock(END)
    client = FakeClient()
    bars = make_source(
        client, symbols_per_request=1, bucket=TokenBucket(2, clock)
    ).fetch_daily_bars(["AAA", "BBB", "CCC"], date(2026, 10, 1), END)
    assert len(bars) == 3
    assert clock.sleep_calls  # third request had to wait


def test_sip_recent_error_mapped_not_retryable() -> None:
    fixture = load("error_sip_recent.json")
    client = FakeClient(error=api_error(fixture["status_code"], fixture["body"]))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    err = info.value
    assert not isinstance(err, RateLimitError)
    assert err.retryable is False
    assert err.provider == "alpaca"
    assert "subscription does not permit querying recent SIP data" in str(err)
    assert "end" in str(err) and "earlier" in str(err)


def test_429_mapped_rate_limit() -> None:
    client = FakeClient(error=api_error(429, {"message": "too many requests."}))
    with pytest.raises(RateLimitError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is True


def test_auth_error_not_retryable() -> None:
    client = FakeClient(error=api_error(401, {"message": "unauthorized."}))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is False
    assert FAKE_KEY not in str(info.value)


def test_5xx_retryable() -> None:
    client = FakeClient(error=api_error(503, {"message": "service unavailable"}))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is True


def test_connection_error_retryable() -> None:
    client = FakeClient(error=requests.ConnectionError("connection reset"))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is True


def test_empty_symbols_makes_no_request() -> None:
    client = FakeClient()
    assert make_source(client).fetch_daily_bars([], date(2026, 10, 1), END) == []
    assert client.requests == []


def test_invalid_symbols_per_request_rejected() -> None:
    with pytest.raises(ValueError):
        make_source(FakeClient(), symbols_per_request=0)


def test_other_4xx_and_unexpected_errors_not_retryable() -> None:
    client = FakeClient(error=api_error(422, {"message": "invalid start"}))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is False and "invalid start" in str(info.value)
    client = FakeClient(error=RuntimeError("boom"))
    with pytest.raises(DataSourceError) as info:
        make_source(client).fetch_daily_bars(["AAA"], date(2026, 10, 1), END)
    assert info.value.retryable is False


def test_repr_hides_credentials() -> None:
    text = repr(make_source(FakeClient()))
    assert FAKE_KEY not in text and FAKE_SECRET not in text
