# uncorrelated-trading

An advisory research bot for a long-only, discretionary strategy. Every trading day it

1. downloads split-adjusted daily bars for a curated universe of energy, metals and mining,
   agriculture and materials equities (plus equity-holding sector ETFs),
2. finds pairs and sets of stocks whose daily log returns are mutually uncorrelated
   (every pair below |rho| = 0.20 over 60 trading days by default, with Spearman and
   long-window stability checks),
3. scores every symbol's momentum (trailing return, 12-1 style skip return and
   volatility-adjusted return, z-scored and blended),
4. reads your Alpaca account (cash, equity, positions, P&L) and flags held names, and
5. emails a plain-text and HTML brief with an `.xlsx` workbook attached (optionally also
   pushing the tables to a Google Sheet).

> **Advisory only. This program never places, modifies or cancels orders.** There is no order
> code anywhere in the repository. It reads market data and account balances; you decide and
> enter every trade by hand. Nothing it produces is investment advice.

Design: [`docs/superpowers/specs/2026-10-14-uncorrelated-trading-design.md`](docs/superpowers/specs/2026-10-14-uncorrelated-trading-design.md).
Configuration reference: [`docs/configuration.md`](docs/configuration.md).
Scheduling and service setup: [`docs/operations.md`](docs/operations.md).

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Account setup](#account-setup)
- [Quick start](#quick-start)
- [As-of semantics](#as-of-semantics)
- [Commands](#commands)
- [Configuration](#configuration)
- [Data notes](#data-notes)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Testing and CI](#testing-and-ci)
- [Publishing to GitHub](#publishing-to-github)
- [License](#license)

## Architecture

```
                 .env (secrets)            config/default.toml (universe, parameters)
                       \                          /
                        v                        v
                     config.Settings      config.AppConfig
                               \              /
                                v            v
   trader CLI (cli.py) ---> app.build_container ---> Container
   trader run (scheduler.py)        |
                                    v
                        jobs.py: fetch -> analyze -> report -> send
                         |          |          |           |
     +-------------------+          |          |           +-------------------+
     v                              v          v                               v
 data/ CompositeDataSource   analysis/     report/                    report/email/
   primary: Alpaca bars      returns       summary (text, HTML)        SmtpSender (Gmail)
   fallback + cross-check:   correlation   workbook (.xlsx)            ResendSender (HTTP)
     Twelve Data             uncorrelated  sheets (Google, optional)   FileSender (outbox)
   offline: CSV file         momentum
     |      (rate limits:    pipeline
     |       ratelimit.py)      ^
     v                          |
 store/ SQLite (data/uncorrelated_trading.sqlite3): bars, calendar cache, API budgets,
        analysis runs, sends, scheduler job runs
     ^
     |
 broker/ AlpacaAccountSource (read-only: account, positions, clock, calendar)
         NullAccountSource (offline)
 calendar.py: CachedCalendar (Alpaca calendar cached in SQLite) or StaticCalendar (NYSE rules)
```

Every step is persisted, so `report` and `send` can be re-run for a stored analysis run,
and the scheduler never sends the same day's brief twice.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python and the dependencies).
- Python 3.11 or newer (the repository pins 3.12 in `.python-version`; `uv` fetches it).
- A machine that is awake at the scheduled times (any Linux, macOS or Windows desktop).
- Free accounts: Alpaca (paper only) is required for live data; Twelve Data, Gmail or
  Resend, and Google Cloud are optional depending on the features you want.

## Account setup

All keys go into `.env` (copied from `.env.example`). `.env` is git-ignored; never commit it.

### Alpaca (required for live data and account reads)

1. Sign up at <https://alpaca.markets> with an email address and choose the
   **paper-only** account. No funding, identity verification or brokerage account is needed.
2. Log in, make sure the dashboard is switched to **Paper Trading** (top-left account
   selector), then open *API Keys* on the paper dashboard and click *Generate New Keys*.
3. Copy the key ID (starts with `PK`) and the secret (shown only once) into `.env`:

   ```
   ALPACA_API_KEY=PK...
   ALPACA_SECRET_KEY=...
   ALPACA_PAPER=true
   ```

The free Basic market-data plan is enough: historical SIP (full-market) daily bars are free
when the request ends more than 15 minutes in the past, which the bot always ensures.
`ALPACA_PAPER=false` only points the read-only account calls at a live account; there is
still no order code.

### Twelve Data (optional: fallback source and close cross-validation)

1. Create a free account at <https://twelvedata.com>.
2. Copy the API key from the dashboard (*API Keys*) into `TWELVEDATA_API_KEY`.

Without this key the bot runs on Alpaca alone; symbols Alpaca does not return are reported
as dropped and no cross-checks are made.

### Email option A: Gmail SMTP

1. Turn on **2-Step Verification** for the Google account
   (<https://myaccount.google.com/security>). App passwords are unavailable without it.
2. Open <https://myaccount.google.com/apppasswords>, create an app password (any name, for
   example `uncorrelated-trading`) and copy the **16-character** password (spaces optional).
3. Set in `.env`:

   ```
   EMAIL_BACKEND=smtp
   EMAIL_FROM=you@gmail.com
   EMAIL_TO=you@gmail.com,partner@example.com
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=abcdabcdabcdabcd
   SMTP_USE_SSL=false
   ```

   Port 587 uses STARTTLS; for port 465 set `SMTP_USE_SSL=true`. Use the app password, never
   your normal Google password.

### Email option B: Resend

1. Sign up at <https://resend.com> and create an API key (*API Keys*, permission
   *Sending access*). It starts with `re_`.
2. Set `EMAIL_BACKEND=resend` and `RESEND_API_KEY=re_...`.
3. **Until you verify a domain in Resend, the account can only send from
   `onboarding@resend.dev` and only to the email address you signed up with.** So for an
   unverified account use `EMAIL_FROM=onboarding@resend.dev` and set `EMAIL_TO` to your own
   signup address. To send to anyone else, add and verify a domain (*Domains*, DNS records)
   and use an address at that domain in `EMAIL_FROM`.

The free plan allows 100 emails per day and 3,000 per month.

### Email option C: file

`EMAIL_BACKEND=file` writes each message as an `.eml` file plus the workbook into
`<UT_DATA_DIR>/outbox/` instead of sending it. Every `--dry-run` uses this sender regardless
of `EMAIL_BACKEND`.

### Google Sheets (optional)

Install the extra (`uv sync --all-extras` already does), then:

1. In <https://console.cloud.google.com> create (or pick) a project.
2. *APIs & Services > Library*: enable the **Google Sheets API** and the **Google Drive API**.
3. *IAM & Admin > Service Accounts*: create a service account (no roles needed), open it,
   *Keys > Add key > Create new key > JSON*, and save the file outside the repository
   (for example `~/.config/uncorrelated-trading/service-account.json`).
4. Create a Google Sheet, click *Share* and add the service account's email
   (`name@project-id.iam.gserviceaccount.com`, shown in the JSON as `client_email`) as
   **Editor**.
5. Set in `.env`:

   ```
   GOOGLE_SERVICE_ACCOUNT_FILE=/home/you/.config/uncorrelated-trading/service-account.json
   GOOGLE_SHEET_ID=<the long id between /d/ and /edit in the sheet URL>
   ```

Sheets push is enabled only when both variables are set. Each publish clears and rewrites
one worksheet per table. A failed publish is logged and does not stop the email.

## Quick start

```bash
uv sync --all-extras
cp .env.example .env            # then replace every placeholder with your own values
uv run trader check-config      # validates .env and the TOML file, lists every problem at once

# Offline smoke test: bundled CSV, no network, no keys needed; writes data/outbox/*.eml + .xlsx
uv run trader --config tests/fixtures/config/offline.toml --data-dir ./data \
    daily --offline --csv tests/fixtures/csv/bars_small.csv --dry-run --as-of 2026-10-13

uv run trader daily --dry-run   # real Alpaca data, email written to data/outbox/ instead of sent
uv run trader daily             # real data, real email
uv run trader run               # long-running scheduler: refresh 16:25, send 07:00 (exchange time)
```

The first live run backfills about 420 calendar days of bars (a handful of Alpaca requests);
later runs fetch only the last week to pick up corrections and splits. To keep the bot running
in the background, see [`docs/operations.md`](docs/operations.md).

## As-of semantics

Every command (and the scheduler) resolves which trading day the analysis is "as of" from the
current time in the exchange timezone (`UT_TIMEZONE`, default `America/New_York`) and the
trading calendar (Alpaca's calendar cached in SQLite, or NYSE rules when that is unavailable;
early closes come from the calendar).

| Mode | When | Data used |
|---|---|---|
| `non_trading_day` | today is a weekend or exchange holiday | through the last trading day before today |
| `pre_open` | trading day, before the open (09:30) | through the previous trading day |
| `intraday` | trading day, between the open and the close | through the previous trading day; today's incomplete session is excluded |
| `post_close_settling` | within `post_close_settle_minutes` (20) after the close | through today, marked **provisional** (closing prints may still be corrected) |
| `post_close` | 20 minutes or more after the close | through today |

If, after the close, fewer than half of the universe have a bar for today yet, the fetch
falls back to the previous trading day and says so in the brief. `--as-of YYYY-MM-DD`
overrides the resolution; a non-trading date snaps back to the previous trading day with a
warning. `uv run trader calendar` shows how "now" resolves.

## Commands

Global options go **before** the command:
`trader [--config PATH] [--data-dir PATH] [--log-level LEVEL] [--log-format text|json] COMMAND ...`.
`--config` overrides `UT_CONFIG_FILE`, `--data-dir` overrides `UT_DATA_DIR`. Command output
goes to standard output, logs to standard error. `uv run trader --help` and
`uv run trader COMMAND --help` list every option.

| Command | Purpose |
|---|---|
| `check-config` | validate the environment and the config file |
| `calendar [--date YYYY-MM-DD]` | show the trading calendar and the as-of resolution |
| `fetch [--as-of D] [--offline --csv PATH]` | fetch daily bars into the local store |
| `analyze [--as-of D] [--offline --csv PATH]` | analyse stored bars and snapshot the account |
| `report (--run-id ID \| --latest) [--out DIR]` | build the brief and workbook for a stored run |
| `send (--run-id ID \| --latest) [--dry-run] [--force]` | email the report for a stored run |
| `daily [--as-of D] [--dry-run] [--offline --csv PATH]` | fetch, analyse, report and send |
| `run` | long-running scheduler |
| `--version` | print the version |

Examples:

```bash
uv run trader check-config
uv run trader calendar                       # today, previous/next trading day, as-of now
uv run trader calendar --date 2026-11-26     # Thanksgiving: not a trading day
uv run trader fetch                          # incremental fetch for the resolved as-of day
uv run trader fetch --as-of 2026-10-09
uv run trader analyze                        # prints the run id, pair count, largest set size
uv run trader analyze --as-of 2026-10-09
uv run trader report --latest                # workbook into data/reports/
uv run trader report --run-id 5d083d1d12fa4045a7b421ad9d898904 --out ~/Desktop
uv run trader send --latest --dry-run        # .eml + .xlsx into data/outbox/, not recorded as sent
uv run trader send --latest                  # exits 5 if this run was already sent ...
uv run trader send --latest --force          # ... unless forced
uv run trader daily --dry-run
uv run trader daily --as-of 2026-10-13
uv run trader --log-level DEBUG --log-format json daily
uv run trader run                            # Ctrl+C (SIGINT) or SIGTERM stops it cleanly
```

`--offline` uses a local CSV file (columns `symbol,day,open,high,low,close,volume`) instead
of Alpaca, no account access and no Twelve Data; Alpaca keys are not needed. `--dry-run`
writes the email to `<data_dir>/outbox/` and does not need email settings.

`trader run` schedules two jobs in the exchange timezone: **refresh** at
`schedule.refresh_time` (16:25) on trading days (fetch, analyse, build the report) and
**send** at `schedule.send_time` (07:00) on `schedule.send_on_days` (sends the latest unsent
report, running the full daily job first if there is none). Each job runs at most once per
local date, even across restarts; a run missed while the machine slept is made up on wake if
it is within the misfire grace (6 h refresh, 3 h send), otherwise it is logged and skipped.

## Configuration

Two sources, kept apart:

- `.env` / environment variables: secrets and machine-specific values (keys, email
  settings, data directory, timezone, log level). Variables already set in the environment
  win over `.env`.
- `config/default.toml`: universe, data, analysis, schedule and report parameters
  (non-secret, committed). Missing keys fall back to built-in defaults; unknown keys are
  errors.

Every variable and key with its default and meaning is listed in
[`docs/configuration.md`](docs/configuration.md). To change the universe or thresholds, copy
`config/default.toml`, edit it and point `UT_CONFIG_FILE` (or `--config`) at the copy.

## Data notes

- **Split-adjusted, not dividend-adjusted.** Bars use Alpaca `adjustment="split"`. Momentum
  over long windows therefore omits dividend yield (the brief says so). Each run re-fetches
  the last 7 calendar days; if a stored close differs from the new one by more than 0.5 %
  (for example after a split) the symbol's full history is re-fetched.
- **SIP 15-minute rule.** The free Alpaca plan serves full-market (SIP) historical data only
  when the query ends more than 15 minutes in the past. Every request ends at
  `min(end of the as-of day, now - 16 minutes)`, so the error "subscription does not permit
  querying recent SIP data" never occurs. Set `data.alpaca_feed = "iex"` only if you want
  the single-exchange IEX feed instead.
- **Rate budgets.** Alpaca's free plan allows 200 historical requests per minute; the bot
  throttles itself to `alpaca_requests_per_minute = 180` and requests up to 40 symbols per
  call (2 requests per day in steady state for 80 symbols). Twelve Data's free plan allows
  8 credits per minute and 800 per day (one credit per symbol); the bot uses at most 7 per
  minute and 750 per day, with the daily count persisted in SQLite so restarts do not reset
  it. At most 20 fallback symbols are fetched per run.
- **Cross-check rotation.** When a Twelve Data key is set, each run compares the last three
  closes of `ceil(N * crosscheck_fraction)` symbols (16 of 80 by default) with Twelve Data,
  rotating deterministically through the universe so each symbol is checked about once a
  week. A difference above 0.5 % is reported as `close_mismatch` in Data Quality; the
  Alpaca value is always the one used. Cross-checks are skipped when fewer than 100 Twelve
  Data credits remain for the day.
- **Missing data is never forward-filled.** A symbol without a bar for the as-of day (after
  one retry and the fallback) is dropped from that run and listed under Data Quality.
- **Statistical caveat.** With a 60-day window the standard error of a correlation is about
  0.13, so |rho| < 0.20 is only about 1.5 standard errors from zero. The workbook therefore
  reports a Fisher-z confidence bound, the Spearman check and whether each set is still
  uncorrelated over the 250-day window.

> **Do not put the data directory on a cloud-synced folder** (Dropbox, OneDrive, iCloud
> Drive, Google Drive). The SQLite database runs in WAL mode, with `-wal` and `-shm` side
> files that sync clients copy independently; this corrupts the database or produces
> conflicting copies. Keep `UT_DATA_DIR` on a local disk and back it up by copying the
> `.sqlite3` file while the bot is stopped. Everything in it can be rebuilt by re-fetching.

## Exit codes

| Code | Meaning | Typical cause |
|---|---|---|
| 0 | success | |
| 1 | unexpected error | a bug; the traceback is logged |
| 2 | configuration or usage error | missing or malformed `.env` variable, invalid TOML value, bad command-line option |
| 3 | data or storage error | Alpaca/Twelve Data unreachable or rejecting the keys, no stored run for `--latest`, database problem |
| 4 | analysis error | fewer than 3 symbols with enough history |
| 5 | report or email error | SMTP/Resend failure (logged as "retryable" when a later retry may succeed), run already sent, workbook error |

Schedulers such as cron can retry on 3 and 5; 2 needs a human.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check-config` exits 2 listing several variables | Each line names one problem; fix them all in `.env`. Placeholders copied from `.env.example` are not valid values. |
| `ALPACA_API_KEY is required but not set` | `.env` must be in the directory you run `trader` from (or export the variables). |
| Alpaca 401 / 403 / "forbidden" | Keys must come from the **paper** dashboard when `ALPACA_PAPER=true`; regenerate if the secret was lost. |
| "subscription does not permit querying recent SIP data" | Should not occur; check the machine's clock (the 16-minute margin depends on it) or set `alpaca_feed = "iex"`. |
| `calendar cache does not cover ...; using static NYSE calendar` | Harmless without Alpaca keys or when offline: NYSE holidays are computed by rule. Online, the cache refreshes automatically. |
| Brief says "provisional" | The run happened within 20 minutes of the close; a later run replaces those bars. |
| Gmail `535 5.7.8 Username and Password not accepted` | Use a 16-character app password with 2-Step Verification on; `SMTP_USERNAME` is the full Gmail address. |
| Resend 403 "You can only send testing emails to your own email address" | Unverified Resend account: send from `onboarding@resend.dev` to your signup address, or verify a domain. |
| Log shows `Google Sheets publish failed` (the email is still sent) | Share the sheet with the service account email as Editor, enable both the Sheets and Drive APIs, check `GOOGLE_SHEET_ID`, and install the extra with `uv sync --all-extras`. |
| `send` exits 5 saying the run was already sent | Sends are recorded once per run; use `--force` to resend deliberately. |
| `no analysis runs stored yet` | Run `trader analyze` or `trader daily` first. |
| `database is locked` or a corrupt database | Only one scheduler should run per data directory; move `UT_DATA_DIR` off cloud-synced folders. |
| Scheduled run never happened | The machine was asleep beyond the misfire grace; see [`docs/operations.md`](docs/operations.md). |
| `ZoneInfoNotFoundError` / unknown timezone on Windows | Reinstall with `uv sync` (the `tzdata` package provides the IANA database). |

Use `--log-level DEBUG` for detail; secrets are redacted from logs.

## Testing and CI

```bash
uv run pytest -m "not integration" -q --cov --cov-fail-under=85   # unit, property, contract, e2e
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -m integration -q    # real APIs; each test skips itself without its keys
```

Test layers: `tests/unit` (every module, fake clients, frozen clock), `tests/property`
(Hypothesis: returns, correlation, uncorrelated sets, momentum, as-of, rate limiter, store,
workbook round-trip), `tests/contract` (adapters against recorded API responses and error
mapping), `tests/e2e` (`trader daily --offline --dry-run` on CSV fixtures) and
`tests/integration` (marker `integration`; Alpaca bars and account, Twelve Data). Unit and
end-to-end tests are blocked from the network.

GitHub Actions:

- `.github/workflows/ci.yml` on every push and pull request: ruff, ruff format, mypy and
  pytest with an 85 % coverage gate on Ubuntu, macOS and Windows with Python 3.11 and 3.12.
- `.github/workflows/integration.yml` nightly at 02:00 UTC on weekdays and on demand: the
  integration tests, using repository secrets.
- `.github/dependabot.yml`: monthly updates for Python dependencies and GitHub Actions.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development workflow.

## Publishing to GitHub

The repository has no remote yet. With the GitHub CLI (`gh auth login` first):

```bash
gh repo create uncorrelated-trading --private --source=. --remote=origin --push
```

Or create an empty private repository named `uncorrelated-trading` on github.com and run:

```bash
git remote add origin git@github.com:<your-user>/uncorrelated-trading.git
git push -u origin main
```

Then add the secrets the integration workflow reads, under *Settings > Secrets and variables
> Actions > New repository secret* (or `gh secret set NAME`):

| Secret | Used by |
|---|---|
| `ALPACA_API_KEY` | Alpaca bars and account integration tests |
| `ALPACA_SECRET_KEY` | Alpaca bars and account integration tests |
| `TWELVEDATA_API_KEY` | Twelve Data integration test |

Tests whose secret is missing are skipped, not failed. The `ci` workflow needs no secrets.

## License

MIT (declared in `pyproject.toml`).
