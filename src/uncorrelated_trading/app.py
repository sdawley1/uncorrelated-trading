"""Composition root: builds every collaborator a command needs from settings and config.

:func:`build_container` is the only place that knows which concrete classes implement the
data, account, calendar, storage and email protocols. Jobs (:mod:`uncorrelated_trading.jobs`)
receive the resulting :class:`Container` and never construct dependencies themselves, so tests
can assemble a container from fakes.

Modes:

* online (default): Alpaca bars as primary, Twelve Data as fallback/cross-check when
  ``TWELVEDATA_API_KEY`` is set, Alpaca account reads, Alpaca-backed cached calendar;
* ``offline``: bars from a local CSV file, no fallback, :class:`NullAccountSource`, static
  calendar (cached rows are still used when present);
* ``dry_run``: email is written to ``<data_dir>/outbox`` by :class:`FileSender` whatever
  ``EMAIL_BACKEND`` says, and sends are not recorded as delivered.

No Alpaca SDK is imported here; the adapters import it lazily on first use.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from uncorrelated_trading.broker.alpaca_account import AlpacaAccountSource
from uncorrelated_trading.broker.base import AccountSource
from uncorrelated_trading.broker.null_account import NullAccountSource
from uncorrelated_trading.calendar import CachedCalendar, TradingCalendar
from uncorrelated_trading.clock import Clock, SystemClock
from uncorrelated_trading.config import AppConfig, Settings
from uncorrelated_trading.data.alpaca_source import AlpacaDataSource
from uncorrelated_trading.data.base import MarketDataSource
from uncorrelated_trading.data.composite import CompositeDataSource
from uncorrelated_trading.data.csv_source import CsvDataSource
from uncorrelated_trading.data.twelvedata_source import TwelveDataSource
from uncorrelated_trading.errors import ConfigError
from uncorrelated_trading.ratelimit import DailyBudget, TokenBucket
from uncorrelated_trading.report.email.base import EmailSender
from uncorrelated_trading.report.email.file_sender import FileSender
from uncorrelated_trading.report.email.resend_sender import ResendSender
from uncorrelated_trading.report.email.smtp_sender import SmtpSender
from uncorrelated_trading.report.sheets import SheetsPublisher
from uncorrelated_trading.store.bars import BarStore
from uncorrelated_trading.store.db import connect, migrate
from uncorrelated_trading.store.runs import RunStore

TWELVEDATA = "twelvedata"
MIN_BUDGET_FOR_CROSSCHECK = 100
OUTBOX_DIRNAME = "outbox"


@dataclass(frozen=True, slots=True)
class Container:
    """Every dependency of the jobs, wired for one command invocation.

    ``dry_run`` is True when the sender was forced to :class:`FileSender` by ``--dry-run``;
    :func:`uncorrelated_trading.jobs.send_job` then neither refuses repeats nor records the
    send in ``email_log``, so a dry run never blocks the real send of the same run.
    """

    settings: Settings
    config: AppConfig
    clock: Clock
    conn: sqlite3.Connection
    bars: BarStore
    runs: RunStore
    calendar: TradingCalendar
    data_source: CompositeDataSource
    account: AccountSource
    sender: EmailSender
    sheets: SheetsPublisher | None
    dry_run: bool = False

    def close(self) -> None:
        """Close the SQLite connection."""
        self.conn.close()


def make_sender(settings: Settings, clock: Clock) -> EmailSender:
    """Build the email sender selected by ``EMAIL_BACKEND``.

    Parameters
    ----------
    settings : Settings
        Runtime settings.
    clock : Clock
        Clock used for send receipts.

    Returns
    -------
    EmailSender
        :class:`SmtpSender`, :class:`ResendSender` or a :class:`FileSender` writing to
        ``<data_dir>/outbox``.
    """
    if settings.email_backend == "smtp":
        return SmtpSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_ssl=settings.smtp_use_ssl,
            clock=clock,
        )
    if settings.email_backend == "resend":
        return ResendSender(settings.resend_api_key or "", clock)
    return FileSender(settings.data_dir / OUTBOX_DIRNAME, clock)


def _today_local(clock: Clock, tz: str) -> date:
    """Return the exchange-local date of ``clock.now()``."""
    now: datetime = clock.now()
    return now.astimezone(ZoneInfo(tz)).date()


def _twelvedata(
    settings: Settings, config: AppConfig, runs: RunStore, clock: Clock
) -> tuple[TwelveDataSource, DailyBudget]:
    """Build the Twelve Data source and its persisted daily budget."""
    tz = settings.timezone
    budget = DailyBudget(
        config.data.twelvedata_requests_per_day,
        used_today=lambda: runs.api_usage_today(TWELVEDATA, _today_local(clock, tz)),
        record=lambda n: runs.add_api_usage(TWELVEDATA, _today_local(clock, tz), n),
    )
    source = TwelveDataSource(
        settings.twelvedata_api_key or "",
        TokenBucket(config.data.twelvedata_requests_per_minute, clock),
        budget,
        tz=tz,
    )
    return source, budget


def build_container(
    settings: Settings,
    config: AppConfig,
    *,
    offline: bool = False,
    dry_run: bool = False,
    csv_path: Path | None = None,
    clock: Clock | None = None,
) -> Container:
    """Open the database and wire every collaborator for one command.

    Parameters
    ----------
    settings : Settings
        Runtime settings (secrets, data directory, time zone).
    config : AppConfig
        Application configuration.
    offline : bool
        Use :class:`CsvDataSource` (``csv_path``) and :class:`NullAccountSource`; no network.
    dry_run : bool
        Force :class:`FileSender` to ``<data_dir>/outbox`` regardless of ``EMAIL_BACKEND``.
    csv_path : Path or None
        CSV bars file; required when ``offline`` and not allowed otherwise.
    clock : Clock or None
        Clock to use; defaults to :class:`SystemClock`.

    Returns
    -------
    Container
        The wired dependencies; call :meth:`Container.close` when done.

    Raises
    ------
    ConfigError
        If ``offline`` and ``csv_path`` disagree.
    StoreError
        If the database cannot be opened or migrated.
    """
    if offline and csv_path is None:
        raise ConfigError(["--offline requires --csv PATH"])
    if csv_path is not None and not offline:
        raise ConfigError(["--csv is only valid together with --offline"])
    clk: Clock = clock if clock is not None else SystemClock()
    tz = settings.timezone
    conn = connect(settings.db_path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    bars = BarStore(conn, clk)
    runs = RunStore(conn, clk)

    primary: MarketDataSource
    fallback: MarketDataSource | None = None
    budget: DailyBudget | None = None
    account: AccountSource
    calendar: CachedCalendar
    if offline and csv_path is not None:
        primary = CsvDataSource(csv_path)
        calendar = CachedCalendar(store=bars, fetch_remote=None, clock=clk, tz=tz)
        account = NullAccountSource(calendar)
    else:
        primary = AlpacaDataSource(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            feed=config.data.alpaca_feed,
            adjustment=config.data.adjustment,
            symbols_per_request=config.data.alpaca_symbols_per_request,
            bucket=TokenBucket(config.data.alpaca_requests_per_minute, clk),
            tz=tz,
        )
        alpaca_account = AlpacaAccountSource(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
            clock=clk,
            tz=tz,
        )
        account = alpaca_account
        calendar = CachedCalendar(
            store=bars, fetch_remote=alpaca_account.trading_days, clock=clk, tz=tz
        )
        if settings.twelvedata_api_key:
            fallback, budget = _twelvedata(settings, config, runs, clk)

    data_source = CompositeDataSource(
        primary,
        fallback,
        tolerance=config.data.close_mismatch_tolerance,
        fallback_max=config.data.twelvedata_fallback_max_per_run,
        crosscheck_fraction=config.data.crosscheck_fraction,
        min_budget_for_crosscheck=MIN_BUDGET_FOR_CROSSCHECK,
        budget=budget,
        sleep=clk.sleep,
    )
    sender = (
        FileSender(settings.data_dir / OUTBOX_DIRNAME, clk)
        if dry_run
        else make_sender(settings, clk)
    )
    sheets = None
    if settings.sheets_enabled and settings.google_service_account_file is not None:
        sheets = SheetsPublisher(
            settings.google_service_account_file, settings.google_sheet_id or ""
        )
    return Container(
        settings=settings,
        config=config,
        clock=clk,
        conn=conn,
        bars=bars,
        runs=runs,
        calendar=calendar,
        data_source=data_source,
        account=account,
        sender=sender,
        sheets=sheets,
        dry_run=dry_run,
    )
