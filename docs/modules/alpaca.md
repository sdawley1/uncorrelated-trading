# Alpaca adapters

Modules: `uncorrelated_trading.data.alpaca_source`, `uncorrelated_trading.broker.alpaca_account`,
`uncorrelated_trading.broker.null_account`. `alpaca-py` is imported lazily inside methods, so the
package imports without it. Clients are built on first use; tests inject fakes via `client=`.
There is no order code: the account adapter only reads.

## `AlpacaDataSource` (implements `MarketDataSource`)

`AlpacaDataSource(api_key, secret_key, feed, adjustment, symbols_per_request, bucket, tz, client=None)`.

`fetch_daily_bars(symbols, start, end_utc)` splits `symbols` into chunks of at most
`symbols_per_request` and, for each chunk, calls `bucket.acquire()` and then
`StockHistoricalDataClient.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk,
timeframe=TimeFrame.Day, start=<start at 00:00 in tz>, end=end_utc, limit=10000,
adjustment=Adjustment(adjustment), feed=DataFeed(feed)))`. The SDK follows `next_page_token`
itself and returns the merged `BarSet`, so each chunk is exactly one client call.

`parse_bars(raw, tz)` accepts `BarSet.data` (`{symbol: [Bar, ...]}`) or a raw JSON page
(`{"bars": {...}, "next_page_token": ...}`); bar fields may be attributes or keys, short
(`t, o, h, l, c, v`) or long names. Alpaca stamps daily bars at midnight exchange-local time in
UTC (`2026-10-13T04:00:00Z`, or `05:00Z` in winter); the trading date is
`timestamp.astimezone(ZoneInfo(tz)).date()`. Volume is rounded to `int`. Malformed bars raise a
non-retryable `DataSourceError`.

## Error mapping (`map_alpaca_error`)

| Failure | Result |
|---|---|
| `APIError` HTTP 429 | `RateLimitError("alpaca")` (retryable) |
| HTTP 401/403 with "subscription" (for example "subscription does not permit querying recent SIP data") | `DataSourceError(retryable=False)` quoting Alpaca and advising to move the request end more than 15 minutes into the past or use `iex` |
| other 401/403 and other 4xx | `DataSourceError(retryable=False)` |
| HTTP 5xx, missing status, `OSError` (includes `requests` connection errors and timeouts) | `DataSourceError(retryable=True)` |
| anything else | `DataSourceError(retryable=False)` |

## `AlpacaAccountSource` (implements `AccountSource`)

`AlpacaAccountSource(api_key, secret_key, paper, clock, tz, client=None)` wraps
`TradingClient(api_key, secret_key, paper=paper)`.

- `account()`: `get_account()` -> `parse_account(raw, taken_at=clock.now(), paper)`. Alpaca's
  string money fields are cast to `float`.
- `positions()`: `get_all_positions()` -> `parse_positions(raw)`; optional fields that Alpaca
  omits become `0.0`.
- `trading_days(start, end)`: `get_calendar(GetCalendarRequest(start, end))` ->
  `parse_calendar(raw, tz)`. Session `open`/`close` are local wall times (`"09:30"`, `"16:00"`,
  or the SDK's naive local datetimes) and become UTC instants. This method is the
  `fetch_remote` callable for `calendar.CachedCalendar`.

Raw inputs may be SDK model objects or JSON dicts. API failures go through `map_alpaca_error`.

## `NullAccountSource`

`NullAccountSource(calendar)` is used with `--offline`: `paper` is True, `account()` and
`positions()` raise `DataSourceError("alpaca", "offline mode: no account access",
retryable=False)`, and `trading_days` delegates to `calendar`.

## Tests

- Contract (`tests/contract/test_alpaca_source.py`, `test_alpaca_account.py`) run against the
  recorded fixtures in `tests/fixtures/alpaca/` (fake keys; XOM, CVX, XLE over five sessions).
- Integration (`tests/integration/test_alpaca_live.py`, marker `integration`) runs only when
  `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set.
