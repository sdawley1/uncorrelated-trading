"""``trader`` command-line interface (spec A10).

Global options come before the subcommand::

    trader [--config PATH] [--data-dir PATH] [--log-level LEVEL] [--log-format {text,json}]
           {check-config,calendar,fetch,analyze,report,send,daily,run} ...

Exit codes: 0 ok, 1 unexpected error (traceback logged), 2 configuration or usage error,
3 data or storage error, 4 analysis error, 5 report or email error. :func:`main` never raises.
Command results go to standard output; logs go to standard error.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from uncorrelated_trading import __version__, app, jobs
from uncorrelated_trading.calendar import CachedCalendar, previous_trading_day
from uncorrelated_trading.config import AppConfig, Settings
from uncorrelated_trading.errors import (
    AnalysisError,
    ConfigError,
    DataSourceError,
    EmailSendError,
    ReportError,
    StoreError,
)
from uncorrelated_trading.logging_setup import add_redaction, configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_DATA = 3
EXIT_ANALYSIS = 4
EXIT_SEND = 5
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SCHEDULER_PLACEHOLDER = "scheduler not yet available"


# ----------------------------------------------------------------------------- parsing


def _iso_date(text: str) -> date:
    """Parse ``YYYY-MM-DD`` for argparse."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        msg = f"expected a date YYYY-MM-DD, got {text!r}"
        raise argparse.ArgumentTypeError(msg) from exc


def _add_as_of(p: argparse.ArgumentParser) -> None:
    """Add ``--as-of``."""
    p.add_argument(
        "--as-of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="analyse through this trading day (non-trading days snap back)",
    )


def _add_offline(p: argparse.ArgumentParser) -> None:
    """Add ``--offline`` and ``--csv``."""
    p.add_argument(
        "--offline", action="store_true", help="use a local CSV and no Alpaca account access"
    )
    p.add_argument("--csv", type=Path, default=None, metavar="PATH", help="CSV bars for --offline")


def _add_run_selector(p: argparse.ArgumentParser) -> None:
    """Add the mutually exclusive ``--run-id`` / ``--latest``."""
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", default=None, help="stored analysis run id")
    group.add_argument("--latest", action="store_true", help="use the most recent run")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``trader`` argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Parser with global options and one subparser per command.
    """
    parser = argparse.ArgumentParser(
        prog="trader",
        description="Advisory analysis of uncorrelated equities with momentum (no orders).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="TOML config (UT_CONFIG_FILE)")
    parser.add_argument("--data-dir", type=Path, default=None, help="data directory (UT_DATA_DIR)")
    parser.add_argument(
        "--log-level", type=str.upper, choices=_LOG_LEVELS, default=None, help="(LOG_LEVEL)"
    )
    parser.add_argument("--log-format", choices=("text", "json"), default=None, help="(LOG_FORMAT)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("check-config", help="validate environment and config file")
    cal = sub.add_parser("calendar", help="show the trading calendar and as-of resolution")
    cal.add_argument("--date", type=_iso_date, default=None, metavar="YYYY-MM-DD")

    fetch = sub.add_parser("fetch", help="fetch daily bars into the local store")
    _add_as_of(fetch)
    _add_offline(fetch)

    analyze = sub.add_parser("analyze", help="analyse stored bars and snapshot the account")
    _add_as_of(analyze)
    _add_offline(analyze)

    report = sub.add_parser("report", help="build the report for a stored run")
    _add_run_selector(report)
    report.add_argument("--out", type=Path, default=None, metavar="DIR", help="workbook directory")

    send = sub.add_parser("send", help="email the report for a stored run")
    _add_run_selector(send)
    send.add_argument("--dry-run", action="store_true", help="write to <data_dir>/outbox")
    send.add_argument("--force", action="store_true", help="send even if already sent")

    daily = sub.add_parser("daily", help="fetch, analyse, report and send")
    _add_as_of(daily)
    daily.add_argument("--dry-run", action="store_true", help="write to <data_dir>/outbox")
    _add_offline(daily)

    sub.add_parser("run", help="long-running scheduler")
    return parser


# ----------------------------------------------------------------------------- helpers


def _out(text: str) -> None:
    """Write one line of command output to standard output."""
    sys.stdout.write(text + "\n")


def _load_settings(
    args: argparse.Namespace, *, require_alpaca: bool, require_email: bool
) -> Settings:
    """Read settings, applying ``--config``/``--data-dir``, then configure logging."""
    env = dict(os.environ)
    if args.config is not None:
        env["UT_CONFIG_FILE"] = str(args.config)
    if args.data_dir is not None:
        env["UT_DATA_DIR"] = str(args.data_dir)
    settings = Settings.from_env(env, require_alpaca=require_alpaca, require_email=require_email)
    configure_logging(args.log_level or settings.log_level, _json_logs(args))
    add_redaction(
        s
        for s in (
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            settings.twelvedata_api_key,
            settings.smtp_password,
            settings.resend_api_key,
        )
        if s
    )
    return settings


def _json_logs(args: argparse.Namespace) -> bool:
    """Return whether JSON log lines were requested (``--log-format`` or ``LOG_FORMAT``)."""
    fmt = args.log_format or os.environ.get("LOG_FORMAT", "text")
    return fmt.lower() == "json"


def _container(
    args: argparse.Namespace, *, require_alpaca: bool, require_email: bool, dry_run: bool
) -> app.Container:
    """Load settings and config and build the container for a command."""
    offline = bool(getattr(args, "offline", False))
    settings = _load_settings(
        args, require_alpaca=require_alpaca and not offline, require_email=require_email
    )
    config = AppConfig.load(settings.config_file)
    return app.build_container(
        settings,
        config,
        offline=offline,
        dry_run=dry_run,
        csv_path=getattr(args, "csv", None),
    )


def _run_id(c: app.Container, args: argparse.Namespace) -> str:
    """Resolve ``--run-id`` / ``--latest`` to a run id."""
    if args.run_id is not None:
        return str(args.run_id)
    latest = c.runs.latest_run_id()
    if latest is None:
        raise StoreError("no analysis runs stored yet; run `trader analyze` or `trader daily`")
    return latest


# ----------------------------------------------------------------------------- commands


def _cmd_check_config(args: argparse.Namespace) -> int:
    """Validate every setting and the config file."""
    settings = _load_settings(args, require_alpaca=True, require_email=True)
    config = AppConfig.load(settings.config_file)
    _out(f"config file:   {settings.config_file}")
    _out(f"data dir:      {settings.data_dir}")
    _out(f"universe:      {len(config.universe.symbols)} symbols")
    _out(f"alpaca:        {'paper' if settings.alpaca_paper else 'live'} account (read only)")
    _out(f"twelve data:   {'enabled' if settings.twelvedata_api_key else 'disabled'}")
    _out(f"email:         {settings.email_backend} -> {', '.join(settings.email_to)}")
    _out(f"google sheets: {'enabled' if settings.sheets_enabled else 'disabled'}")
    _out(f"timezone:      {settings.timezone}")
    _out("configuration OK")
    return EXIT_OK


def _cmd_calendar(args: argparse.Namespace) -> int:
    """Show whether a date trades and how the current as-of resolves."""
    settings = _load_settings(args, require_alpaca=False, require_email=False)
    config = AppConfig.load(settings.config_file)
    c = app.build_container(settings, config, dry_run=True)
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        # Without Alpaca keys, never attempt the remote calendar: cache or static rules only.
        c = replace(c, calendar=CachedCalendar(c.bars, None, c.clock, settings.timezone))
    try:
        calendar = c.calendar
        tz = ZoneInfo(settings.timezone)
        day = args.date or c.clock.now().astimezone(tz).date()
        rows = calendar.trading_days(day, day)
        source = getattr(calendar, "source", "static")
        if rows:
            row = rows[0]
            _out(
                f"{day.isoformat()}: trading day, open {row.open_utc.astimezone(tz):%H:%M}, "
                f"close {row.close_utc.astimezone(tz):%H:%M} ({settings.timezone})"
            )
        else:
            _out(f"{day.isoformat()}: not a trading day")
        _out(f"previous trading day: {previous_trading_day(day, calendar).isoformat()}")
        upcoming = calendar.trading_days(day + timedelta(days=1), day + timedelta(days=10))
        if upcoming:
            _out(f"next trading day: {upcoming[0].day.isoformat()}")
        _out(f"calendar source: {source}")
        if args.date is None:
            as_of = jobs.resolve_run_as_of(c, None)
            _out(f"as-of now: {as_of.day.isoformat()} ({as_of.mode}): {as_of.note}")
    finally:
        c.close()
    return EXIT_OK


def _cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch bars for the resolved as-of."""
    c = _container(args, require_alpaca=True, require_email=False, dry_run=True)
    try:
        as_of = jobs.resolve_run_as_of(c, args.as_of)
        outcome = jobs.fetch_job(c, as_of)
    finally:
        c.close()
    issues = sum(1 for q in outcome.quality if q.status != "ok")
    _out(
        f"as of {outcome.as_of.day.isoformat()} ({outcome.as_of.mode}): "
        f"{outcome.bars_written} bars written, {issues} data-quality issue(s)"
    )
    if outcome.fell_back_to_previous_day:
        _out(f"note: {outcome.as_of.note}")
    return EXIT_OK


def _cmd_analyze(args: argparse.Namespace) -> int:
    """Analyse stored bars for the resolved as-of."""
    c = _container(args, require_alpaca=True, require_email=False, dry_run=True)
    try:
        as_of = jobs.resolve_run_as_of(c, args.as_of)
        result = jobs.analyze_job(c, as_of, ())
    finally:
        c.close()
    size = result.largest_set.size if result.largest_set else 0
    _out(
        f"run {result.run_id}: as of {result.as_of.day.isoformat()}, "
        f"{len(result.quality.analysed)} of {len(result.universe)} symbols analysed, "
        f"{len(result.pairs)} uncorrelated pairs, largest set size {size}"
    )
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    """Build the report for a stored run and write the workbook."""
    c = _container(args, require_alpaca=False, require_email=False, dry_run=True)
    try:
        run_id = _run_id(c, args)
        out_dir = args.out if args.out is not None else c.settings.data_dir / "reports"
        bundle = jobs.report_job(c, run_id, out_dir)
    finally:
        c.close()
    _out(f"run {run_id}: {bundle.message.subject}")
    _out(f"workbook: {out_dir / bundle.message.attachments[0].filename}")
    if bundle.sheets_url:
        _out(f"google sheet: {bundle.sheets_url}")
    return EXIT_OK


def _cmd_send(args: argparse.Namespace) -> int:
    """Send the report for a stored run."""
    c = _container(args, require_alpaca=False, require_email=not args.dry_run, dry_run=args.dry_run)
    try:
        run_id = _run_id(c, args)
        bundle = jobs.report_job(c, run_id)
        receipt = jobs.send_job(c, bundle, force=args.force)
    finally:
        c.close()
    _out(f"run {run_id}: sent via {receipt.backend} ({receipt.message_id})")
    return EXIT_OK


def _cmd_daily(args: argparse.Namespace) -> int:
    """Fetch, analyse, report and send."""
    c = _container(args, require_alpaca=True, require_email=not args.dry_run, dry_run=args.dry_run)
    try:
        receipt = jobs.daily_job(c, args.as_of)
    finally:
        c.close()
    _out(f"daily run complete: {receipt.backend} ({receipt.message_id})")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    """Report that the scheduler is not wired in yet (Task 16)."""
    _out(_SCHEDULER_PLACEHOLDER)
    return EXIT_OK


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "check-config": _cmd_check_config,
    "calendar": _cmd_calendar,
    "fetch": _cmd_fetch,
    "analyze": _cmd_analyze,
    "report": _cmd_report,
    "send": _cmd_send,
    "daily": _cmd_daily,
    "run": _cmd_run,
}


# ----------------------------------------------------------------------------- entry point


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``trader`` command line.

    Parameters
    ----------
    argv : Sequence[str] or None
        Arguments without the program name; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        Process exit code: 0 ok, 1 unexpected error, 2 configuration or usage error,
        3 data or storage error, 4 analysis error, 5 report or email error.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 0 for --help/--version and 2 on usage errors
        return exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    # Logging for errors raised before settings are read; reconfigured once they are.
    configure_logging(args.log_level or os.environ.get("LOG_LEVEL", "INFO"), _json_logs(args))
    try:
        return _COMMANDS[args.command](args)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except (DataSourceError, StoreError) as exc:
        logger.error("data error: %s", exc)
        return EXIT_DATA
    except AnalysisError as exc:
        logger.error("analysis error: %s", exc)
        return EXIT_ANALYSIS
    except (EmailSendError, ReportError) as exc:
        retry = " (retryable)" if getattr(exc, "retryable", False) else ""
        logger.error("report/send error%s: %s", retry, exc)
        return EXIT_SEND
    except Exception:
        logger.exception("unexpected error")
        return EXIT_UNEXPECTED
