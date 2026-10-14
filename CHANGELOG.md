# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- A run is never labelled with an as-of day it has no bars for: `run_analysis` raises
  "no bars for as-of day D; last available day is X", and the fetch falls back to the
  previous session in every mode (not only after the close) when fewer than half of the
  universe have a bar for the day.
- `trader run` retries jobs that fail with a transient error every 5 minutes within the
  misfire grace (status `retry`); `running` rows left by a crash are reclaimed after 15
  minutes, so a restart within the grace still sends that day's email.
- `--as-of` is compared with today in the exchange timezone; `--as-of` today resolves like a
  run without override, so an in-progress session is never stored as a close.
- The cached Alpaca calendar refreshes again in long-running processes (remote attempts at
  most once a day) instead of degrading to the static calendar after about 60 days.
- Price windows use the days on which at least half of the symbols have a bar, so one stray
  bar on an extra day no longer drops every other symbol.
- The 20-symbol Twelve Data fallback cap applies per run, across per-symbol retries, and
  retries are no longer cross-checked.
- `send --latest` and the scheduler pick the run with the latest as-of day, not the most
  recently created one.
- `send --force` uses a fresh idempotency key (`-f<N>` suffix), so Resend really resends.
- Secrets are masked in logged exception tracebacks.
- Data Quality reports the data source's real primary-failure and fallback-call counts.
- The scheduler publishes the Google Sheet once a day (by `send`), not twice.
- CI runs every step on the matrix Python version.

### Added

- The default configuration ships inside the package and is used when
  `./config/default.toml` is absent; `check-config` shows which file was read.
- Placeholder values copied from `.env.example` are rejected; with Sheets enabled the
  service-account file must exist.
- `LICENSE` (MIT).

## [0.1.0] - 2026-10-14

First release. Advisory only: the program reads market data and account balances and never
places orders.

### Added

- Project scaffold (uv, hatchling, `src/` layout), frozen data contracts (`models.py`),
  exception hierarchy (`errors.py`), logging with secret redaction and optional JSON lines.
- Configuration: secrets and machine settings from the environment / `.env`
  (`config.Settings`), parameters from TOML (`config.AppConfig`, `config/default.toml` with an
  80-symbol energy, metals, agriculture and materials universe); all problems reported at once.
- Trading calendar and as-of resolution with five modes (`pre_open`, `intraday`,
  `post_close_settling`, `post_close`, `non_trading_day`), Alpaca calendar cached in SQLite
  and a rule-based NYSE fallback calendar with early closes.
- SQLite store (WAL): bars with upsert and restatement detection, calendar cache, API usage
  budgets, analysis runs, report sends and scheduler job runs.
- Market data: Alpaca daily bars (split-adjusted SIP, SIP 15-minute rule respected, chunked
  multi-symbol requests, pagination), Twelve Data fallback and rotating close cross-check,
  CSV source for offline runs, token-bucket rate limiting and persisted daily budgets, retry
  with exponential backoff.
- Read-only Alpaca account adapter (account, positions, clock, calendar) and an offline null
  account.
- Analysis: log returns, Pearson and Spearman correlation over 60/120/250-day windows,
  uncorrelated pairs, exact enumeration of uncorrelated sets of size 3 and 4, time-bounded
  largest-set search, set diagnostics (Fisher-z bound, minimum eigenvalue, Spearman check,
  long-window stability) and a composite momentum score with direction labels.
- Reporting: plain-text and HTML brief, `.xlsx` workbook (Summary, correlation sheets,
  Uncorrelated Sets, Momentum, Positions, Data Quality, Parameters), optional Google Sheets
  publisher (`sheets` extra).
- Email: SMTP (STARTTLS or SSL; Gmail app passwords), Resend HTTP API with idempotency key,
  and a file outbox used by `--dry-run`; each run is sent at most once unless forced.
- `trader` CLI: `check-config`, `calendar`, `fetch`, `analyze`, `report`, `send`, `daily`,
  `run` (long-running scheduler), with `--offline`, `--dry-run`, `--as-of` and documented
  exit codes.
- Test suite: unit, Hypothesis property, contract, end-to-end and integration layers with a
  network guard; CI on Ubuntu, macOS and Windows with Python 3.11 and 3.12 and an 85 %
  coverage gate; nightly integration workflow; Dependabot.
- Documentation: README with account walkthroughs, `docs/configuration.md`,
  `docs/operations.md` (systemd, launchd, Task Scheduler, cron), `CONTRIBUTING.md`.

[Unreleased]: https://github.com/OWNER/uncorrelated-trading/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/uncorrelated-trading/releases/tag/v0.1.0
