# Configuration reference

Configuration comes from two places, kept strictly apart:

1. **Environment variables** (secrets and machine-specific values), read by
   `config.Settings.from_env`. A `.env` file in the working directory is loaded with
   python-dotenv; variables already set in the process environment always win over it.
   `.env` is git-ignored; start from `.env.example`.
2. **A TOML file** (non-secret parameters), read by `config.AppConfig.load`. The default is
   `config/default.toml` in the working directory; when that file does not exist, the
   identical copy shipped inside the package (`uncorrelated_trading/config_default.toml`) is
   used, so `trader` works outside the repository. Choose another file with
   `UT_CONFIG_FILE` or `trader --config PATH`. `trader check-config` prints the file used.
   Missing sections and keys fall back to the defaults below, so a partial file is valid.
   Unknown sections or keys, wrong types and out-of-range values are errors.

Both loaders report every problem at once (`trader check-config` prints them and exits 2).

## Environment variables

Booleans accept `1/true/yes/on` and `0/false/no/off` (case-insensitive). Blank values count
as unset. Values left as `.env.example` placeholders are errors ("KEY looks like a
placeholder from .env.example"): a run of at least 8 `X` or `x` characters, optionally after
a short prefix such as `PK` or `re_`, and any path starting with `/path/to/`. This is checked
for the Alpaca keys (when required), the email secrets (when email is required),
`TWELVEDATA_API_KEY`, `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_FILE`.

| Variable | Required when | Default | Meaning |
|---|---|---|---|
| `ALPACA_API_KEY` | always, except with `--offline` and for `calendar`, `report`, `send` | none | Alpaca API key ID from the paper dashboard (starts with `PK`). |
| `ALPACA_SECRET_KEY` | same as `ALPACA_API_KEY` | none | Alpaca secret key. |
| `ALPACA_PAPER` | never | `true` | `true` reads the paper account; `false` points the read-only account calls at a live account. There is no order code either way. |
| `TWELVEDATA_API_KEY` | never | unset | Enables Twelve Data as fallback source and the close cross-check. |
| `EMAIL_BACKEND` | for `check-config`, `daily`, `send` without `--dry-run` | `file` when not required | `smtp`, `resend` or `file`. |
| `EMAIL_FROM` | as `EMAIL_BACKEND` | none | Sender address (must contain `@`). With an unverified Resend account: `onboarding@resend.dev`. |
| `EMAIL_TO` | as `EMAIL_BACKEND` | none | Comma-separated recipient addresses. |
| `SMTP_HOST` | never | `smtp.gmail.com` | SMTP server. |
| `SMTP_PORT` | never | `587` | SMTP port (positive integer); 587 = STARTTLS, 465 = implicit SSL. |
| `SMTP_USERNAME` | `EMAIL_BACKEND=smtp` (and email required) | unset | SMTP login (for Gmail, the full address). |
| `SMTP_PASSWORD` | `EMAIL_BACKEND=smtp` (and email required) | unset | SMTP password; for Gmail a 16-character app password. |
| `SMTP_USE_SSL` | never | `false` | `true` uses implicit SSL (port 465) instead of STARTTLS. |
| `RESEND_API_KEY` | `EMAIL_BACKEND=resend` (and email required) | unset | Resend API key (`re_...`). |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | never | unset | Path to a Google service-account JSON key. Sheets push is on only when this and `GOOGLE_SHEET_ID` are both set; the file must then exist. |
| `GOOGLE_SHEET_ID` | never | unset | ID of the target Google Sheet (shared with the service account as Editor). |
| `UT_DATA_DIR` | never | `data` | Directory for the SQLite database `uncorrelated_trading.sqlite3`, `outbox/` and `reports/`. Overridden by `--data-dir`. Keep it off cloud-synced folders. |
| `UT_CONFIG_FILE` | never | `config/default.toml` if it exists, else the packaged copy | TOML configuration file. Overridden by `--config`. |
| `UT_TIMEZONE` | never | `America/New_York` | Exchange timezone (IANA name) for the schedule and as-of resolution. |
| `LOG_LEVEL` | never | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Overridden by `--log-level`. |
| `LOG_FORMAT` | never | `text` | `text` or `json` (one JSON object per line). Overridden by `--log-format`. Read by the CLI, not by `Settings`. |

"Email required" means the command will actually send: `check-config`, `daily` and `send`
without `--dry-run`. `--dry-run` always writes to `<UT_DATA_DIR>/outbox/` and needs no email
settings.

Integration tests additionally read `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER` and
`TWELVEDATA_API_KEY` directly from the environment and skip themselves when they are absent.

## TOML file

### `[universe]`

| Key | Default | Meaning |
|---|---|---|
| `symbols` | the 80 tickers in `config/default.toml` | Symbols to analyse: 2 to 200 unique upper-case tickers (`A-Z`, digits, `.` and `-`, up to 10 characters). |
| `excluded_patterns` | `["USO","UNG","GLD","SLV","DBC","BNO","UCO","SCO","PPLT","PALL","CORN","WEAT","SOYB","CPER"]` | Shell-style patterns (`fnmatch`, case-sensitive) that no symbol may match; guards against futures- or metal-backed funds. |

### `[data]`

| Key | Default | Meaning |
|---|---|---|
| `backfill_calendar_days` | `420` | History requested for a symbol with no stored bars (covers 250 trading days with margin). Integer >= 1. |
| `overlap_calendar_days` | `7` | Calendar days re-fetched before the last stored bar on each run, to catch corrections and splits. Integer >= 0. |
| `adjustment` | `"split"` | Alpaca price adjustment: `raw`, `split`, `dividend` or `all`. |
| `alpaca_feed` | `"sip"` | Alpaca data feed: `sip` (full market, free for queries ending more than 15 minutes ago) or `iex`. |
| `alpaca_requests_per_minute` | `180` | Token-bucket rate for Alpaca data requests (the free plan allows 200). |
| `alpaca_symbols_per_request` | `40` | Maximum symbols per multi-symbol bars request. |
| `twelvedata_requests_per_minute` | `7` | Token-bucket rate for Twelve Data (the free plan allows 8 credits per minute). |
| `twelvedata_requests_per_day` | `750` | Daily Twelve Data budget persisted in SQLite (the free plan allows 800). |
| `twelvedata_fallback_max_per_run` | `20` | Maximum symbols fetched from Twelve Data as fallback per run (across the main fetch and the per-symbol retries). Integer >= 0. |
| `crosscheck_fraction` | `0.2` | Share of the universe whose closes are cross-checked against Twelve Data per run (`ceil(N * fraction)` symbols, rotating daily). In [0, 1]. |
| `close_mismatch_tolerance` | `0.005` | Relative close difference that flags `close_mismatch` (cross-check) or a restatement (stored vs re-fetched). In [0, 1]. |
| `post_close_settle_minutes` | `20` | Minutes after the close during which today's bars are marked provisional (`post_close_settling`). Integer >= 0. |

### `[analysis]`

| Key | Default | Meaning |
|---|---|---|
| `primary_window` | `60` | Trading-day window (log returns) for the headline correlations, pairs and sets. Must be one of `windows`; integer >= 2. |
| `windows` | `[60, 120, 250]` | All correlation windows computed; the longest is used for the stability flag. Non-empty list of positive integers. |
| `tau` | `0.20` | Pearson threshold: a pair is uncorrelated when abs(rho) < tau. In (0, 1). |
| `tau_spearman` | `0.30` | Spearman cross-check threshold reported per set (`passes_spearman`). In (0, 1). |
| `max_set_size_exact` | `4` | Largest set size enumerated exactly (sizes 3 to this). Integer >= 2. |
| `max_sets_per_size` | `50` | Cap on sets kept per size during enumeration. |
| `search_time_budget_s` | `20.0` | Time budget in seconds for set enumeration and the largest-set search; when it expires the largest set is reported as not proven maximal. > 0. |
| `momentum_k` | `20` | Trailing window (days) for the short return and volatility. |
| `momentum_long` | `120` | Long window (days) for the skip return; must exceed `momentum_skip`. |
| `momentum_skip` | `20` | Most recent days excluded from the long return (12-1 style). |
| `momentum_weights` | `[0.4, 0.3, 0.3]` | Weights of the z-scored trailing return, skip return and volatility-adjusted return in the composite. Three non-negative numbers, not all zero. |
| `direction_threshold` | `0.5` | Composite z above `+threshold` is `up`, below `-threshold` is `down`, otherwise `flat`. >= 0. |

`min_history_fraction` from early drafts is still accepted (a number in [0, 1]) for
compatibility but has no effect: a symbol is dropped whenever it lacks a full window of bars.

### `[schedule]`

Used by `trader run`. Times are `HH:MM` in the exchange timezone (`UT_TIMEZONE`).

| Key | Default | Meaning |
|---|---|---|
| `refresh_time` | `"16:25"` | Daily refresh (fetch, analyse, build report) on trading days. |
| `send_time` | `"07:00"` | Daily send of the latest unsent report. |
| `send_on_days` | `"weekdays"` | Days the send job runs: `weekdays`, `trading_days` or `every_day`. |
| `poll_seconds` | `30` | Scheduler wake-up interval; the wall clock is re-read each time, so sleep/wake is noticed within this delay. |
| `send_misfire_grace_hours` | `3` | A send missed (machine off or asleep) is still made if the scheduler notices within this many hours; a send failing with a transient error is retried every 5 minutes within the same window. Integer >= 0. |
| `refresh_misfire_grace_hours` | `6` | Same for the refresh job. Integer >= 0. |

### `[report]`

| Key | Default | Meaning |
|---|---|---|
| `top_pairs` | `15` | Uncorrelated pairs listed in the brief. |
| `top_sets_per_size` | `5` | Best sets listed per size (3 and 4). |
| `top_momentum` | `10` | Symbols listed at each end of the momentum ranking. |
| `subject_prefix` | `"[uncorrelated-trading]"` | Prefix of the email subject (may be empty). |
