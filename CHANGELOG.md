# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
