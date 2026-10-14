# Trading calendar, as-of resolution and rate limiting

Modules: `uncorrelated_trading.calendar`, `uncorrelated_trading.ratelimit`.

## As-of resolution

`resolve_as_of(now_utc, calendar, tz, settle_minutes, calendar_source)` decides which trading
day's close a run includes. With `today` the exchange-local date of `now_utc`:

| Situation | `mode` | `day` | `provisional` |
|---|---|---|---|
| `today` is not a trading day | `non_trading_day` | last trading day before today | no |
| before the open | `pre_open` | previous trading day | no |
| open <= now < close | `intraday` | previous trading day (today excluded) | no |
| close <= now < close + `settle_minutes` | `post_close_settling` | today | yes |
| now >= close + `settle_minutes` | `post_close` | today | no |

Open and close come from the calendar's `TradingDay` rows, so 13:00 early closes need no
special handling.

`data_request_end(as_of, tz, now_utc)` returns
`min(23:59:59 of as_of.day in tz, now_utc - 16 min)` so data requests never fall inside the
Alpaca SIP 15-minute window.

`snap_as_of(requested, calendar, now_utc)` implements `--as-of YYYY-MM-DD`: a trading day is
used as is; a non-trading day snaps back to the previous trading day with a logged warning; a
future date raises `ValueError`.

## Calendars

- `StaticCalendar(tz)`: weekdays minus rule-computed NYSE holidays
  (<https://www.nyse.com/markets/hours-calendars>), sessions 09:30-16:00 local, 13:00 on the
  day after Thanksgiving and on July 3 / December 24 when they are weekdays and not holidays.
  Good Friday uses the Anonymous Gregorian Easter algorithm. Ad hoc closures (for example the
  national day of mourning on 2025-01-09) are not known to it.
- `CachedCalendar(store, fetch_remote, clock, tz, max_age_days=30)`: serves rows from a
  `CalendarStore` (structurally satisfied by `BarStore`). It calls
  `fetch_remote(today - 400 d, today + 60 d)` at most once per instance, when the cache was
  never refreshed, is older than `max_age_days`, or does not cover the requested range. If the
  cache then still does not cover the range (remote missing or failing), the range is served
  from `StaticCalendar` and `source` becomes `"static"` (logged as a warning); a stale cache
  that still covers the range keeps being served with `source = "alpaca"`. `source` is set by
  each query, so read it after resolving and pass it on as `calendar_source`.

## Rate limiting

- `TokenBucket(rate_per_minute, clock)`: sliding window of acquisition times; `acquire()`
  sleeps through `clock.sleep` until fewer than `rate_per_minute` acquisitions lie in the last
  60 s. Any half-open 60 s window contains at most `rate_per_minute` acquisitions.
- `DailyBudget(limit, used_today, record)`: `remaining()` is `max(0, limit - used_today())`;
  `spend(n)` raises `RateLimitError("twelvedata")` without recording when
  `used_today() + n > limit`, otherwise calls `record(n)`. In production the callables are
  bound to `BarStore.api_usage_today` / `BarStore.add_api_usage` for the provider and day.
