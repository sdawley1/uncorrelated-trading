# Part A: Design Spec

## A1. Context

The user wants a personal research bot for a long-only, discretionary strategy: hold stocks whose returns are mutually uncorrelated, and use momentum to judge which are likely to rise or fall. In the first version the bot is **advisory only**: it computes and delivers the numbers; the user enters and exits positions by hand. The bot must run on any personal desktop (Linux, macOS, Windows), either as one long-running process or as one-shot commands, cost nothing in data fees, keep secrets out of git, and be built like a robust application with documentation, a layered test suite, and CI.

Decisions fixed during brainstorming (18 questions answered 2026-10-14):

| Topic | Decision |
|---|---|
| Exchange / data | Alpaca (paper-only account is sufficient) for daily bars and account reads, behind an abstract interface; Twelve Data as secondary free data source for fallback and close cross-validation. No IBKR, Tradier, Public.com, yfinance. |
| Universe | Curated list in a committed TOML config (40 to 80 tickers). Equity-holding sector ETFs allowed (XLE, XOP, XME, GDX, MOO); futures- or metal-backed ETFs excluded (USO, UNG, GLD, SLV, DBC). |
| Orders | **None in v1.** No order code at all. Account, cash, P&L and positions are read and reported. |
| Returns | Daily close-to-close log returns only. Default primary window 60 trading days; also 120 and 250. |
| Uncorrelated criterion | Every pair in a set has Pearson abs(rho) < tau (default 0.20); Spearman cross-check (default 0.30). Exact enumeration for sizes 2 to 4, greedy plus time-capped exact search for the largest set. Smallest eigenvalue of the sub-correlation matrix reported per set. |
| Momentum | Composite z-scored blend: trailing k-day return, longer-window return skipping the most recent s days (12-1 analogue), volatility-adjusted return. Weights configurable. Advisory only. |
| Email | Two selectable backends: SMTP with STARTTLS (stdlib; Gmail app password documented) and Resend HTTP API. Default send 07:00 America/New_York. |
| Attachment | `.xlsx` with sheets Summary, Correlation Matrix, Uncorrelated Sets, Momentum, Positions, Data Quality. Google Sheets push via gspread service account, enabled only when env vars present. |
| Report scope | Analysis plus current positions, cash, equity, P&L; held names flagged with their momentum and set membership. |
| Scheduling | Long-running `trader run` with stdlib scheduler, plus one-shot subcommands with as-of logic: during a session use data through the previous trading day; after the close use data through today. |
| Storage / CI | SQLite via stdlib. GitHub Actions matrix (ubuntu, macos, windows) running ruff, mypy, pytest with coverage. Private GitHub repo created via `gh`. |
| Python | `requires-python = ">=3.11"`, `.python-version` = 3.12. |

## A2. Verified external facts (as of 2026-10-14)

- **Alpaca Basic (free) plan**: 200 historical API calls per minute; real-time data IEX only; full-market SIP historical data is free when the query `end` is more than 15 minutes in the past (otherwise error "subscription does not permit querying recent SIP data"). Paper Only Account needs only an email address, no funding, base URL `https://paper-api.alpaca.markets`. `alpaca-py` 0.44.0 (2026-08-11), Python 3.10 to 3.14.
- **Alpaca SDK surface used**: `StockHistoricalDataClient(api_key, secret_key).get_stock_bars(StockBarsRequest(symbol_or_symbols, timeframe=TimeFrame.Day, start, end, limit, adjustment, feed))` returning `BarSet` with `.data: dict[str, list[Bar]]`, `Bar(symbol, timestamp, open, high, low, close, volume, trade_count, vwap)`. `TradingClient(api_key, secret_key, paper=True)` with `get_account() -> TradeAccount` (string-typed `cash, equity, buying_power, portfolio_value, last_equity, currency`), `get_all_positions() -> list[Position]` (string-typed `qty, avg_entry_price, market_value, cost_basis, unrealized_pl, unrealized_plpc, current_price, lastday_price, change_today`), `get_clock() -> Clock(timestamp, is_open, next_open, next_close)`, `get_calendar(GetCalendarRequest(start, end)) -> list[Calendar(date, open, close)]`. `DataFeed.SIP/IEX/DELAYED_SIP`, `Adjustment.SPLIT`.
- **Twelve Data free**: 8 credits per minute, 800 per day, one credit per symbol. `GET https://api.twelvedata.com/time_series?symbol=XOM&interval=1day&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&outputsize=5000&apikey=...` returns `{meta, values:[{datetime, open, high, low, close, volume}], status:"ok"}`, all numbers as strings; errors `{code, message, status:"error"}`, 429 on limit.
- **Resend**: `POST https://api.resend.com/emails`, `Authorization: Bearer`, JSON `{from, to, subject, text, html, attachments:[{filename, content(base64), content_type}]}`, 40 MB limit, free 3,000/month and 100/day, response `{id}`. Optional `Idempotency-Key` header.
- **Gmail SMTP**: requires 2-step verification and a 16-character app password; `smtp.gmail.com` port 587 STARTTLS or 465 SSL.
- **Google Sheets API**: free; 300 reads and 300 writes per minute per project, 60 per user per minute. Service account JSON key; the target sheet must be shared with the service account email.
- **Ruled out**: Finnhub free tier no longer reliably serves US stock candles; Stooq throttles with "Exceeded the daily hits limit"; yfinance has persistent undocumented 429s; IBKR paper requires a funded Pro account plus a running Java gateway; Public.com has no sandbox; SendGrid free plan ended 2025-05-27.

## A3. Repository layout

```
uncorrelated-trading/
  pyproject.toml            uv project, deps, ruff/mypy/pytest config, [project.scripts] trader = "uncorrelated_trading.cli:main"
  .python-version           3.12
  uv.lock
  .gitignore                .env, data/, .venv/, __pycache__/, *.sqlite3, dist/, .coverage, htmlcov/
  .env.example              every env var with comment and placeholder
  README.md                 setup, account creation walkthroughs, commands, config reference, scheduling on 3 OSes
  CHANGELOG.md
  config/default.toml       universe + analysis + schedule + report parameters
  docs/superpowers/specs/2026-10-14-uncorrelated-trading-design.md
  docs/superpowers/plans/2026-10-14-uncorrelated-trading.md
  .github/workflows/ci.yml  lint, type, unit/property tests, coverage (matrix)
  .github/workflows/integration.yml  nightly, uses secrets if present
  src/uncorrelated_trading/
    __init__.py             __version__
    models.py               shared frozen dataclasses (the contract)
    errors.py               exception hierarchy
    config.py               Settings (env) + AppConfig (TOML) loading and validation
    logging_setup.py        configure_logging(level, json: bool)
    calendar.py             TradingCalendar protocol, StaticCalendar, resolve_as_of()
    ratelimit.py            TokenBucket, DailyBudget
    data/
      __init__.py
      base.py               MarketDataSource protocol
      alpaca_source.py      AlpacaDataSource
      twelvedata_source.py  TwelveDataSource
      csv_source.py         CsvDataSource (offline fixtures)
      composite.py          CompositeDataSource (primary, fallback, cross-validation)
    broker/
      __init__.py
      base.py               AccountSource protocol
      alpaca_account.py     AlpacaAccountSource (account, positions, clock, calendar)
      null_account.py       NullAccountSource (offline)
    store/
      __init__.py
      db.py                 connect(), migrate(), schema SQL
      bars.py               BarStore
      runs.py               RunStore (analysis runs, sets, momentum, snapshots, email log, job runs, api usage)
    analysis/
      __init__.py
      returns.py            price matrix + log returns
      correlation.py        Pearson/Spearman matrices
      uncorrelated.py       pair/n-tuple discovery, eigen diagnostics
      momentum.py           composite momentum
      pipeline.py           run_analysis() -> AnalysisResult
    report/
      __init__.py
      summary.py            render_text(), render_html()
      workbook.py           build_workbook() -> bytes
      sheets.py             SheetsPublisher (optional extra)
      email/
        __init__.py
        base.py             EmailSender protocol, EmailMessage, Attachment, SendReceipt
        smtp_sender.py      SmtpSender
        resend_sender.py    ResendSender
        file_sender.py      FileSender (writes .eml + attachment to disk; dry runs)
    jobs.py                 fetch_job(), analyze_job(), report_job(), send_job(), daily_job()
    scheduler.py            Scheduler (stdlib), job table, idempotency via RunStore
    cli.py                  argparse entry point
  tests/
    conftest.py             fixtures: tmp db, sample bars, fake clients, frozen clock
    fixtures/               recorded JSON (alpaca bars, account, positions, calendar; twelvedata; resend), CSV bars
    unit/                   one test module per source module
    property/               hypothesis tests
    contract/               adapters against recorded fixtures
    integration/            real APIs, skipped without env (marker `integration`)
    e2e/                    `trader daily --dry-run` on CSV source
```

## A4. Shared contracts (`models.py`)

All are `@dataclass(frozen=True, slots=True)`; `CorrelationMatrix` additionally sets `eq=False` because it holds a numpy array (compare with the helper `correlation_matrices_equal(a, b, atol=1e-12)` defined in `models.py`). Money and prices are `float` (converted from Alpaca's strings at the adapter boundary). Dates are `datetime.date`; instants are tz-aware `datetime` in UTC. Sub-agents must not change this file; needed changes go to the lead.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
import numpy as np

AsOfMode = Literal["pre_open", "intraday", "post_close_settling", "post_close", "non_trading_day"]
Direction = Literal["up", "down", "flat"]

@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str                      # "alpaca" | "twelvedata" | "csv"

@dataclass(frozen=True, slots=True)
class TradingDay:
    day: date
    open_utc: datetime
    close_utc: datetime

@dataclass(frozen=True, slots=True)
class AsOf:
    day: date                        # last trading day whose close is included
    mode: AsOfMode
    now_utc: datetime
    provisional: bool                # True in post_close_settling: today's bars may still be revised
    calendar_source: Literal["alpaca", "static"]
    note: str                        # human-readable reason

@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    taken_at: datetime
    cash: float
    equity: float
    buying_power: float
    portfolio_value: float
    last_equity: float
    currency: str
    paper: bool

@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float
    change_today: float

@dataclass(frozen=True, slots=True)
class CorrelationMatrix:
    window: int                      # trading days of returns used
    method: Literal["pearson", "spearman"]
    symbols: tuple[str, ...]
    values: np.ndarray               # shape (n, n), symmetric, unit diagonal
    n_obs: int
    start_day: date
    end_day: date

@dataclass(frozen=True, slots=True)
class UncorrelatedSet:
    symbols: tuple[str, ...]         # sorted
    size: int
    max_abs_rho: float               # Pearson, primary window
    mean_abs_rho: float
    max_abs_rho_ci_upper: float      # Fisher-z 95 % upper bound on the largest |rho| in the set
    min_eigenvalue: float            # of Pearson sub-matrix
    spearman_max_abs_rho: float
    passes_spearman: bool            # spearman_max_abs_rho < tau_spearman
    stable_in_long_window: bool      # also pairwise below tau in the longest configured window
    mean_momentum_z: float           # mean composite momentum of members (filled by pipeline)

@dataclass(frozen=True, slots=True)
class MomentumScore:
    symbol: str
    trailing_return: float           # log return over k days
    skip_return: float               # log return over [t-L, t-s]
    vol_adjusted_return: float       # trailing_return / (sigma_daily * sqrt(k))
    realized_vol: float              # annualised, sqrt(252) * sigma_daily
    composite_z: float
    rank: int                        # 1 = strongest
    direction: Direction

@dataclass(frozen=True, slots=True)
class SymbolQuality:
    symbol: str
    status: Literal["ok", "dropped_missing", "dropped_short_history", "fallback_used", "close_mismatch"]
    detail: str
    bars_available: int
    source: str

@dataclass(frozen=True, slots=True)
class DataQualityReport:
    requested: tuple[str, ...]
    analysed: tuple[str, ...]
    per_symbol: tuple[SymbolQuality, ...]
    primary_source_failures: int
    fallback_calls: int

@dataclass(frozen=True, slots=True)
class AnalysisParams:
    primary_window: int              # 60
    windows: tuple[int, ...]         # (60, 120, 250)
    tau: float                       # 0.20
    tau_spearman: float              # 0.30
    max_set_size_exact: int          # 4
    max_sets_per_size: int           # 50
    search_time_budget_s: float      # 20.0
    momentum_k: int                  # 20
    momentum_long: int               # 120
    momentum_skip: int               # 20
    momentum_weights: tuple[float, float, float]   # (0.4, 0.3, 0.3)
    direction_threshold: float       # 0.5

@dataclass(frozen=True, slots=True)
class AnalysisResult:
    run_id: str                      # uuid4 hex
    as_of: AsOf
    created_at: datetime
    params: AnalysisParams
    universe: tuple[str, ...]
    correlations: tuple[CorrelationMatrix, ...]      # pearson+spearman per window
    pairs: tuple[UncorrelatedSet, ...]               # size 2, ascending max_abs_rho
    sets_by_size: dict[int, tuple[UncorrelatedSet, ...]]   # 3..max_set_size_exact
    largest_set: UncorrelatedSet | None
    largest_set_exact: bool                          # False if time budget hit
    momentum: tuple[MomentumScore, ...]              # sorted by rank
    quality: DataQualityReport

@dataclass(frozen=True, slots=True)
class PortfolioContext:
    account: AccountSnapshot | None
    positions: tuple[PositionSnapshot, ...]
    error: str | None                # set when account read failed

@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str

@dataclass(frozen=True, slots=True)
class EmailMessage:
    sender: str
    recipients: tuple[str, ...]
    subject: str
    text_body: str
    html_body: str
    attachments: tuple[Attachment, ...]
    idempotency_key: str

@dataclass(frozen=True, slots=True)
class SendReceipt:
    backend: str
    message_id: str
    sent_at: datetime

@dataclass(frozen=True, slots=True)
class ReportBundle:
    result: AnalysisResult
    portfolio: PortfolioContext
    message: EmailMessage
    sheets_url: str | None
```

Protocols (in `data/base.py`, `broker/base.py`, `report/email/base.py`, `calendar.py`):

```python
class MarketDataSource(Protocol):
    name: str
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...
    # end_utc comes from calendar.data_request_end() so the SIP 15-minute rule is respected

class AccountSource(Protocol):
    paper: bool
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> list[PositionSnapshot]: ...
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

class EmailSender(Protocol):
    name: str
    def send(self, message: EmailMessage) -> SendReceipt: ...

class TradingCalendar(Protocol):
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...
```

Exceptions (`errors.py`): `UncorrelatedTradingError` base; `ConfigError`; `DataSourceError(provider, retryable: bool)`; `RateLimitError(DataSourceError)`; `StoreError`; `AnalysisError`; `ReportError`; `EmailSendError(backend, retryable)`.

## A5. Configuration

`.env` (git-ignored, loaded with python-dotenv, never overriding already-set OS env vars). `config.Settings.from_env()` raises `ConfigError` listing every missing or malformed key at once.

| Variable | Required | Notes |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | yes | Paper keys from the paper dashboard |
| `ALPACA_PAPER` | no, default `true` | `false` only switches account reads to the live endpoint; there is no order code |
| `TWELVEDATA_API_KEY` | no | enables fallback source |
| `EMAIL_BACKEND` | yes | `smtp` or `resend` or `file` |
| `EMAIL_FROM`, `EMAIL_TO` | yes | `EMAIL_TO` comma-separated |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_SSL` | when `smtp` | defaults `smtp.gmail.com`, `587`, `false` |
| `RESEND_API_KEY` | when `resend` | |
| `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID` | no | both set enables Sheets push |
| `UT_DATA_DIR` | no, default `./data` | SQLite file `uncorrelated_trading.sqlite3` and dry-run outputs |
| `UT_CONFIG_FILE` | no, default `config/default.toml` | |
| `UT_TIMEZONE` | no, default `America/New_York` | exchange timezone used for schedule and as-of |
| `LOG_LEVEL` | no, default `INFO` | |

`config/default.toml`:

```toml
[universe]
# Equity-only energy, metals/mining, agriculture, materials, plus equity-holding sector ETFs.
symbols = [
  # Integrated / E&P / services / refining / midstream
  "XOM","CVX","COP","EOG","OXY","DVN","FANG","APA","CTRA","EQT","AR","RRC",
  "SLB","HAL","BKR","MPC","PSX","VLO","KMI","WMB","OKE","LNG","TRGP",
  # Solar / clean energy equities
  "FSLR","ENPH",
  # Metals and mining
  "FCX","NEM","SCCO","AA","CLF","NUE","STLD","RS","MP","ALB","SQM",
  "AEM","WPM","RGLD","HL","CDE","PAAS","VALE","RIO","BHP",
  # Agriculture and ag-chemicals
  "ADM","BG","CF","MOS","NTR","CTVA","FMC","DE","AGCO","TSN","INGR","DAR",
  # Industrial materials and chemicals
  "LIN","APD","DD","DOW","LYB","CE","EMN","VMC","MLM",
  # Equity-holding sector ETFs (no futures or physical commodity funds)
  "XLE","XOP","OIH","XME","GDX","GDXJ","SIL","MOO","XLB","COPX","URA","TAN","ICLN","PICK",
]
excluded_patterns = ["USO","UNG","GLD","SLV","DBC","BNO","UCO","SCO","PPLT","PALL","CORN","WEAT","SOYB","CPER"]

[data]
backfill_calendar_days = 420       # covers 250 trading days with margin
overlap_calendar_days = 7          # re-fetch this much history each run to catch corrections and splits
adjustment = "split"
alpaca_feed = "sip"
alpaca_requests_per_minute = 180
alpaca_symbols_per_request = 40
twelvedata_requests_per_minute = 7
twelvedata_requests_per_day = 750
twelvedata_fallback_max_per_run = 20
crosscheck_fraction = 0.2          # share of the universe cross-checked against Twelve Data per run
close_mismatch_tolerance = 0.005   # 0.5 % relative difference flags close_mismatch / restated
post_close_settle_minutes = 20     # before 16:20 ET today's bars are marked provisional

[analysis]
primary_window = 60
windows = [60, 120, 250]
tau = 0.20
tau_spearman = 0.30
max_set_size_exact = 4
max_sets_per_size = 50
search_time_budget_s = 20.0
momentum_k = 20
momentum_long = 120
momentum_skip = 20
momentum_weights = [0.4, 0.3, 0.3]
direction_threshold = 0.5

[schedule]
refresh_time = "16:25"             # local exchange time, trading days only
send_time = "07:00"                # local exchange time
send_on_days = "weekdays"          # "weekdays" | "trading_days" | "every_day"
poll_seconds = 30
send_misfire_grace_hours = 3
refresh_misfire_grace_hours = 6

[report]
top_pairs = 15
top_sets_per_size = 5
top_momentum = 10
subject_prefix = "[uncorrelated-trading]"
```

`AppConfig.load(path)` validates: symbols unique, upper-case, 2 to 200 of them, none matching `excluded_patterns`; `tau` in (0, 1); `primary_window in windows`; `momentum_long > momentum_skip >= 1`; weights length 3 and non-negative; times parse as `HH:MM`.

## A6. Trading calendar and as-of resolution (`calendar.py`)

`resolve_as_of(now_utc, calendar, tz, settle_minutes) -> AsOf` is the single source of truth for every command and for the scheduler:

1. Convert `now_utc` to exchange local time; let `today` be the local date.
2. Fetch `calendar.trading_days(today - 10 days, today + 1 day)`. The `CachedCalendar` serves this from SQLite table `calendar_days`, refreshing from Alpaca `get_calendar(today - 400 d, today + 60 d)` at most once per 30 days. If Alpaca is unreachable and the cache does not cover the range, it falls back to `StaticCalendar` (`calendar_source="static"`, logged as a warning and shown in Data Quality).
3. `today` not a trading day: `mode="non_trading_day"`, `day=` last trading day before today.
4. `now < open`: `mode="pre_open"`, `day=` previous trading day.
5. `open <= now < close`: `mode="intraday"`, `day=` previous trading day; note says today's session is excluded.
6. `close <= now < close + settle_minutes`: `mode="post_close_settling"`, `day=today`, `provisional=True`.
7. `now >= close + settle_minutes`: `mode="post_close"`, `day=today`.

Early closes (13:00) come from the calendar rows, so half-days are handled without special cases. `StaticCalendar` computes NYSE holidays by rule (New Year's Day, MLK Day, Presidents' Day, Good Friday via Easter computus, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas; Saturday observed Friday, Sunday observed Monday) and early closes (day after Thanksgiving, July 3 and December 24 when weekdays), and is unit-tested against the known 2025 and 2026 holiday dates.

Data requests use `end = min(end of as_of.day in exchange tz, now_utc - 16 minutes)` so the Alpaca SIP 15-minute rule is never hit. Alpaca stamps daily bars at the session date's midnight ET, so a bar for `as_of.day` is returned by such a query as soon as it exists. If, in `post_close_settling` or `post_close` mode, fewer than half of the universe have a bar for `as_of.day`, the fetch job falls back to the previous trading day and records the reason in the AsOf note (bars not yet published).

`--as-of YYYY-MM-DD` on any command overrides the resolution; a non-trading date is snapped back to the previous trading day with a warning.

## A7. Data layer

- **Incremental fetch** (`jobs.fetch_job`): for each symbol, `last = BarStore.last_day(symbol)`; `start = (last - overlap_calendar_days) if last else as_of.day - backfill_calendar_days` (overlap default 7 calendar days, about 5 trading days, to pick up late corrections and detect splits). Symbols are grouped by identical `(start, end)` and Alpaca is queried in chunks of at most `alpaca_symbols_per_request` (40) symbols per multi-symbol request (`limit=10000`, follow `next_page_token`). Steady state for 80 symbols is 2 requests; a full backfill is about 6. Alpaca daily bar timestamps (midnight ET, e.g. `2026-10-13T04:00:00Z`) are converted to the exchange-local date in the adapter. Bars are upserted (`INSERT OR REPLACE`). Before upserting, overlapping stored closes are compared with fetched closes; a relative difference above `close_mismatch_tolerance` on any overlap day marks the symbol `restated` and triggers one full re-fetch of that symbol only. Any symbol whose returned bars stop before `as_of.day` is retried once; if still missing it is recorded as `dropped_missing` for this run (never forward-filled).
- **Fallback and cross-validation** (`CompositeDataSource`): symbols for which Alpaca returned nothing or raised a retryable error after 3 attempts are fetched from Twelve Data one symbol per request under the token bucket and daily budget, capped at `twelvedata_fallback_max_per_run` (20); they are marked `fallback_used`. Cross-validation rotates through the universe: each run checks `ceil(N / 5)` symbols (deterministic rotation keyed on the as-of day) by fetching their last 3 closes from Twelve Data (`outputsize=3`), so every symbol is cross-checked about weekly at roughly 16 credits per night; a relative mismatch above tolerance yields `close_mismatch` with both values in `detail`. The primary's value is always used. Cross-checks are skipped when the remaining daily budget is below 100 credits or no Twelve Data key is configured.
- **Rate limiting** (`ratelimit.py`): `TokenBucket(rate_per_minute)` blocking `acquire()`; `DailyBudget(provider, limit, store)` persisted in `api_usage(provider, day, count)`. Property test: never more than `rate` acquisitions in any 60-second window under a fake clock.
- **Retry**: 3 attempts with exponential backoff (1 s, 2 s, 4 s) on `retryable` errors (HTTP 429, 5xx, connection errors); never on 4xx auth errors.

## A8. Analysis

- **Returns** (`returns.py`): `build_price_matrix(bars, symbols, end_day, window) -> pd.DataFrame` (index trading days ascending, columns symbols, close prices, last `window + 1` rows). Symbols with any NaN in the window are dropped and reported. `log_returns(prices) -> pd.DataFrame` = `np.log(prices).diff().dropna(how="all")`.
- **Correlation** (`correlation.py`): `correlation_matrix(returns, method) -> CorrelationMatrix` via `DataFrame.corr`; enforces symmetry (average with transpose), unit diagonal, clipping to [-1, 1].
- **Uncorrelated sets** (`uncorrelated.py`): Build graph on analysed symbols with an edge where `abs(rho) >= tau`. An uncorrelated set is an independent set. 
  - `find_pairs(corr, tau)`: all i<j with `abs(rho) < tau`, ascending.
  - `enumerate_independent_sets(adjacency, size, cap, deadline)`: depth-first extension with candidate lists (clique enumeration in the complement) returning up to `cap` sets per size ordered by `max_abs_rho`; sizes 3..`max_set_size_exact`. Worst case for 80 symbols at size 4 is C(80,4) = 1.58e6 candidate checks, trivially within 20 s in numpy; the `deadline` guards pathological configurations (tau near 1).
  - `largest_independent_set(adjacency, deadline)`: greedy minimum-degree heuristic first, then Bron-Kerbosch with pivoting on the complement graph bounded by `deadline`; `largest_set_exact=False` when the deadline fires.
  - `set_diagnostics(set, pearson, spearman, long_pearson) -> UncorrelatedSet` computes `max_abs_rho`, `mean_abs_rho`, `max_abs_rho_ci_upper` (Fisher z: `tanh(atanh(r) + 1.96 / sqrt(n_obs - 3))`), `min_eigenvalue` (`np.linalg.eigvalsh`), `spearman_max_abs_rho`, `passes_spearman`, `stable_in_long_window` (all pairs below tau in the longest window's Pearson matrix; False if any member is missing there). The report footnote states that with a 60-day window the standard error of rho is about 0.13, so tau = 0.2 is roughly 1.5 standard errors from zero; the CI column and the stability flag exist to make that visible.
  - Ranking within each size: `max_abs_rho` ascending, then `min_eigenvalue` descending, then `mean_momentum_z` descending.
  - Property tests: every reported set satisfies pairwise bound; every subset of a reported set is itself uncorrelated; enumeration counts equal brute-force `itertools.combinations` for n <= 12; results are invariant to symbol ordering.
- **Momentum** (`momentum.py`): with log-return matrix `R` (length >= `momentum_long`): `trailing = R.iloc[-k:].sum()`; `skip = R.iloc[-L:-s].sum()`; `sigma = R.iloc[-k:].std(ddof=1)`; `vol_adj = trailing / (sigma * sqrt(k))` (0 when sigma == 0); `realized_vol = sigma * sqrt(252)`; z-score each across symbols (population std; 0 when std == 0), winsorised to [-3, 3]; `composite = w . (z_trailing, z_skip, z_voladj) / sum(w)`; rank descending (1 = strongest); direction by `direction_threshold`. Bars are split-adjusted, not dividend-adjusted, so the long-window return omits dividend yield; the report footnote says so. Property tests: z-scores have mean ~0; adding a constant to all prices (in log space) leaves ranks unchanged; permutation invariance.
- **Pipeline** (`pipeline.py`): `run_analysis(bars, universe, as_of, params, clock) -> AnalysisResult`; the only function orchestration calls. Raises `AnalysisError` if fewer than 3 symbols survive.

## A9. Reporting

- **Brief** (`summary.py`): `render_text(bundle_inputs) -> str` and `render_html(...) -> str`, deterministic given inputs (no timestamps other than `created_at`). Sections: header (as-of day, mode note, universe size, analysed size), data quality one-liner plus dropped symbols, top uncorrelated pairs (`report.top_pairs`), best sets for sizes 3..4 (`top_sets_per_size`), largest set with diagnostics and exactness flag, momentum top/bottom `top_momentum`, holdings table (symbol, qty, P&L, momentum rank, direction, "in largest set" flag), account summary, parameters footer. HTML uses inline CSS tables only.
- **Workbook** (`workbook.py`): `build_workbook(result, portfolio) -> bytes` with sheets `Summary`, `Correlation 60` (one sheet per window, Pearson; Spearman on `Spearman 60`), `Uncorrelated Sets`, `Momentum`, `Positions`, `Data Quality`, `Parameters`. Header row bold, frozen panes, number formats `0.000` for rho and `0.00%` for returns, conditional colour scale on correlation sheets. Filename `uncorrelated_YYYY-MM-DD.xlsx`.
- **Email**: `SmtpSender` (stdlib `smtplib`, `email.message.EmailMessage`, STARTTLS on 587 or `SMTP_SSL` on 465, 30 s timeout); `ResendSender` (`requests.post`, base64 attachment, `Idempotency-Key` = `message.idempotency_key`, 30 s timeout, retry once on 5xx/429); `FileSender` writes `<data_dir>/outbox/<key>.eml` plus the attachment and returns a receipt (used by `--dry-run`, e2e tests and CI). `idempotency_key = f"{run_id}-{recipients_hash}"`.
- **Google Sheets** (`sheets.py`, extra `sheets`): `SheetsPublisher(service_account_file, sheet_id).publish(result, portfolio) -> url`. For each table, `worksheet.clear()` then one `update()` call (values as lists), so a full publish is about 8 write calls, far under 60 per minute. Import of gspread happens inside the class so the core app runs without the extra. Feasible and included; README documents the 5 setup steps (create GCP project, enable Sheets and Drive APIs, create service account and JSON key, share the sheet with the service-account email as Editor, set the two env vars).

## A10. Orchestration

- `jobs.py`: pure functions taking explicit dependencies (`settings, config, store, data_source, account_source, sender, clock`), returning records. `daily_job` = fetch -> analyze -> report -> send, each step persisted so `report`/`send` can be rerun for a `run_id`.
- `scheduler.py`: `Scheduler(jobs, clock, store)`; each job has `next_fire(now_utc) -> datetime` computed by building the exchange-local wall time with `zoneinfo` and converting to UTC (DST-safe), a `misfire_grace` (default 3 h for `send`, 6 h for `refresh`), and `run_on_startup_if_missed`. The loop waits on a `threading.Event` for at most `poll_seconds`, then re-reads the wall clock, so laptop sleep is handled: on wake a job within its grace runs, otherwise a miss is logged. A job fires at most once per local date (`job_runs(job, run_date)` primary key), so restarts never double-send; exceptions are logged and the loop continues; SIGINT/SIGTERM set the event and exit cleanly. Jobs: `refresh` at `schedule.refresh_time` on trading days (fetch + analyze + build report), `send` at `schedule.send_time` on `send_on_days` (sends the latest unsent report; if none exists it runs `daily_job` first).
- `cli.py` (`trader`): `check-config`, `calendar [--date]`, `fetch [--as-of]`, `analyze [--as-of]`, `report --run-id|--latest [--out DIR]`, `send --run-id|--latest [--dry-run]`, `daily [--as-of] [--dry-run]`, `run`. `--dry-run` swaps in `FileSender` and `NullAccountSource` is used only when Alpaca keys are absent and `--offline` is given. Exit codes: 0 ok, 2 config error, 3 data error, 4 analysis error, 5 send error.
- Logging: stdlib `logging`, human format by default, `LOG_FORMAT=json` option, secrets never logged (Settings `__repr__` masks).

## A11. Testing and CI

| Layer | Location | Scope |
|---|---|---|
| Unit | `tests/unit` | every module; fake clients injected; frozen clock |
| Property | `tests/property` | returns, correlation, uncorrelated sets, momentum, as-of, token bucket, store upsert idempotency, workbook round-trip |
| Contract | `tests/contract` | adapters parse recorded JSON in `tests/fixtures`; error mapping (401, 429, 5xx) |
| Integration | `tests/integration` | marker `integration`; skipped unless env present; Alpaca bars for 3 symbols, account read, Twelve Data one symbol, Resend/SMTP only with `UT_RUN_EMAIL_TESTS=1` |
| End-to-end | `tests/e2e` | `trader daily --dry-run --offline` on CSV fixtures: workbook opens with openpyxl, `.eml` parses, sheets present |

Coverage gate 85 % (`--cov-fail-under=85`), `ruff check` + `ruff format --check`, `mypy src` with `strict = true` (pandas-stubs; `alpaca` ignored via `ignore_missing_imports` only if untyped). CI matrix: `ubuntu-latest`, `macos-latest`, `windows-latest` x Python 3.11, 3.12 using `astral-sh/setup-uv`. Integration workflow runs nightly at 02:00 UTC and on `workflow_dispatch`, passing repository secrets.

## A12. Operational notes for the README

- Account creation walkthroughs: Alpaca paper-only signup and key generation; Twelve Data free key; Gmail app password; Resend key and sender verification; Google service account.
- Running: `uv sync --all-extras`, `cp .env.example .env`, `uv run trader check-config`, `uv run trader daily --dry-run`, then `uv run trader run`.
- Keeping it running: systemd user unit (Linux), launchd plist (macOS), Task Scheduler XML (Windows) examples that call `uv run trader run`; alternative cron/Task Scheduler lines calling `uv run trader daily` at 16:30 and `uv run trader send --latest` at 07:00.
- CPU: analysis is single-threaded numpy; `OMP_NUM_THREADS` default left to the user; no multiprocessing.

---

