"""Jobs: fetch, analyze, report, send and the composed daily run (spec A6, A7, A10).

Every job takes a :class:`~uncorrelated_trading.app.Container` holding its dependencies and
persists its outcome, so ``report`` and ``send`` can be re-run later for a stored ``run_id``.

* :func:`fetch_job` performs the incremental bar fetch with overlap, restatement detection,
  one retry for symbols missing the as-of day, and the fallback to the previous trading day
  when most bars for the as-of day are unavailable.
* :func:`analyze_job` runs the analysis on stored bars and snapshots the account.
* :func:`report_job` renders the brief and workbook into an email (and optionally Sheets).
* :func:`send_job` sends it once per run (unless forced) and logs the delivery.
* :func:`daily_job` chains all four.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from uncorrelated_trading import __version__
from uncorrelated_trading.analysis.pipeline import run_analysis
from uncorrelated_trading.app import Container
from uncorrelated_trading.calendar import (
    data_request_end,
    previous_trading_day,
    resolve_as_of,
    snap_as_of,
)
from uncorrelated_trading.errors import (
    ConfigError,
    DataSourceError,
    EmailSendError,
    ReportError,
    StoreError,
)
from uncorrelated_trading.models import (
    AnalysisResult,
    AsOf,
    Attachment,
    Bar,
    EmailMessage,
    PortfolioContext,
    ReportBundle,
    SendReceipt,
    SymbolQuality,
)
from uncorrelated_trading.report.inputs import BriefInputs
from uncorrelated_trading.report.summary import render_html, render_text, subject_line
from uncorrelated_trading.report.workbook import build_workbook, workbook_filename

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_POST_CLOSE_MODES = ("post_close_settling", "post_close")
_CALENDAR_PRIME_DAYS = 10


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Result of :func:`fetch_job`.

    ``as_of`` is the as-of actually used, which differs from the requested one when
    ``fell_back_to_previous_day`` is True. ``quality`` carries every fetch-time record
    (``fallback_used``, ``close_mismatch``, ``dropped_missing``).
    """

    as_of: AsOf
    bars_written: int
    quality: tuple[SymbolQuality, ...]
    fell_back_to_previous_day: bool


# ----------------------------------------------------------------------------- as-of


def resolve_run_as_of(c: Container, override: date | None) -> AsOf:
    """Resolve the as-of for a run, honouring an explicit ``--as-of`` override.

    Parameters
    ----------
    c : Container
        Dependencies (clock, calendar, settings, config).
    override : date or None
        Requested as-of date (see :func:`~uncorrelated_trading.calendar.snap_as_of`): a
        non-trading date is snapped back to the previous session, and today resolves like a
        run without override.

    Returns
    -------
    AsOf
        The resolved as-of.

    Raises
    ------
    ConfigError
        If ``override`` lies in the future.
    """
    now = c.clock.now()
    if override is not None:
        try:
            return snap_as_of(
                override,
                c.calendar,
                now,
                c.settings.timezone,
                c.config.data.post_close_settle_minutes,
            )
        except ValueError as exc:
            raise ConfigError([str(exc)]) from exc
    tz = c.settings.timezone
    today = now.astimezone(ZoneInfo(tz)).date()
    # CachedCalendar.source is only meaningful after a query; prime it first.
    c.calendar.trading_days(today - timedelta(days=_CALENDAR_PRIME_DAYS), today)
    raw_source = getattr(c.calendar, "source", "static")
    return resolve_as_of(
        now,
        c.calendar,
        tz,
        c.config.data.post_close_settle_minutes,
        calendar_source="alpaca" if raw_source == "alpaca" else "static",
    )


# ----------------------------------------------------------------------------- fetch


def _rel_diff(stored: float, fetched: float) -> float:
    """Relative difference of ``fetched`` from ``stored`` (inf when stored is 0 and differs)."""
    if stored == fetched:
        return 0.0
    if stored == 0:
        return float("inf")
    return abs(fetched - stored) / abs(stored)


def _restatement(c: Container, symbol: str, fetched: Sequence[Bar]) -> SymbolQuality | None:
    """Compare fetched closes with stored closes on overlapping days.

    Returns a ``close_mismatch`` record for the first overlap day whose relative difference
    exceeds ``close_mismatch_tolerance``, else None.
    """
    if not fetched:
        return None
    days = [b.day for b in fetched]
    stored = {b.day: b.close for b in c.bars.load([symbol], min(days), max(days))}
    tolerance = c.config.data.close_mismatch_tolerance
    for bar in sorted(fetched, key=lambda b: b.day):
        old = stored.get(bar.day)
        if old is not None and _rel_diff(old, bar.close) > tolerance:
            return SymbolQuality(
                symbol=symbol,
                status="close_mismatch",
                detail=(
                    f"restated: stored={old:.6g} fetched={bar.close:.6g} on {bar.day.isoformat()}"
                ),
                bars_available=len(fetched),
                source=bar.source,
            )
    return None


def _start_for(c: Container, symbol: str, as_of_day: date) -> date:
    """Return the incremental fetch start for ``symbol`` (overlap or full backfill)."""
    data = c.config.data
    last = c.bars.last_day(symbol)
    if last is not None:
        return last - timedelta(days=data.overlap_calendar_days)
    return as_of_day - timedelta(days=data.backfill_calendar_days)


def _fetch_symbols(
    c: Container, symbols: Sequence[str], as_of_day: date, end_utc: datetime
) -> tuple[int, list[SymbolQuality]]:
    """Fetch ``symbols`` incrementally, detect restatements and upsert; return count, quality."""
    groups: dict[date, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(_start_for(c, symbol, as_of_day), []).append(symbol)
    written = 0
    quality: list[SymbolQuality] = []
    restated: list[str] = []
    for start in sorted(groups):
        res = c.data_source.fetch(groups[start], start, end_utc, as_of_day)
        quality.extend(res.quality)
        by_symbol: dict[str, list[Bar]] = {}
        for bar in res.bars:
            by_symbol.setdefault(bar.symbol, []).append(bar)
        for symbol in groups[start]:
            mismatch = _restatement(c, symbol, by_symbol.get(symbol, []))
            if mismatch is not None:
                logger.warning("%s: %s; re-fetching full history", symbol, mismatch.detail)
                quality.append(mismatch)
                restated.append(symbol)
        written += c.bars.upsert(res.bars)
    if restated:
        full_start = as_of_day - timedelta(days=c.config.data.backfill_calendar_days)
        res = c.data_source.fetch(restated, full_start, end_utc, as_of_day)
        quality.extend(q for q in res.quality if q.status != "close_mismatch")
        written += c.bars.upsert(res.bars)
    return written, quality


def _with_bar_on(c: Container, symbols: Sequence[str], day: date) -> set[str]:
    """Return the subset of ``symbols`` with a stored bar on ``day``."""
    return {b.symbol for b in c.bars.load(list(symbols), day, day)}


def fallback_note(as_of: AsOf) -> str:
    """Return the note recorded when ``as_of.day`` lacks bars and the previous session is used.

    Parameters
    ----------
    as_of : AsOf
        The as-of whose day has too few bars.

    Returns
    -------
    str
        ``"bars for D unavailable; using previous session"``; in post-close modes the reason
        ``"(not yet published)"`` is added after ``unavailable``.
    """
    reason = " (not yet published)" if as_of.mode in _POST_CLOSE_MODES else ""
    return f"bars for {as_of.day.isoformat()} unavailable{reason}; using previous session"


def fetch_job(c: Container, as_of: AsOf) -> FetchOutcome:
    """Incrementally fetch daily bars for the universe and store them (spec A6, A7).

    For each symbol the request starts ``overlap_calendar_days`` before its last stored day
    (or ``backfill_calendar_days`` before ``as_of.day`` when it has none); symbols sharing a
    start are fetched together, ending at :func:`data_request_end`. A fetched close differing
    from the stored close by more than ``close_mismatch_tolerance`` marks the symbol
    ``close_mismatch`` ("restated") and triggers one full re-fetch of it. In every mode, when
    fewer than half the universe have a bar for ``as_of.day``, the as-of moves to the previous
    trading day with the note of :func:`fallback_note`. Symbols still lacking a bar for the
    final day are retried once and then recorded as ``dropped_missing`` (never forward-filled).

    Parameters
    ----------
    c : Container
        Dependencies.
    as_of : AsOf
        Requested as-of.

    Returns
    -------
    FetchOutcome
        The as-of used, rows written, quality records and whether the fallback fired.
    """
    universe = list(c.config.universe.symbols)
    tz = c.settings.timezone
    end_utc = data_request_end(as_of, tz, c.clock.now())
    written, quality = _fetch_symbols(c, universe, as_of.day, end_utc)

    fell_back = False
    present = _with_bar_on(c, universe, as_of.day)
    if 2 * len(present) < len(universe):
        previous = previous_trading_day(as_of.day, c.calendar)
        logger.warning(
            "only %d of %d symbols have a bar for %s; falling back to %s",
            len(present),
            len(universe),
            as_of.day,
            previous,
        )
        note = fallback_note(as_of)
        as_of = replace(
            as_of,
            day=previous,
            provisional=False,
            note=f"{as_of.note}; {note}" if as_of.note else note,
        )
        fell_back = True
        present = _with_bar_on(c, universe, as_of.day)

    missing = [s for s in universe if s not in present]
    if missing:
        logger.info("retrying %d symbol(s) missing %s: %s", len(missing), as_of.day, missing)
        end_utc = data_request_end(as_of, tz, c.clock.now())
        for symbol in missing:
            n, q = _fetch_symbols(c, [symbol], as_of.day, end_utc)
            written += n
            quality.extend(q)
        present = _with_bar_on(c, universe, as_of.day)

    final = _final_quality(c, universe, present, quality, as_of.day)
    return FetchOutcome(
        as_of=as_of,
        bars_written=written,
        quality=final,
        fell_back_to_previous_day=fell_back,
    )


def _final_quality(
    c: Container,
    universe: Sequence[str],
    present: set[str],
    quality: Sequence[SymbolQuality],
    day: date,
) -> tuple[SymbolQuality, ...]:
    """Put a ``dropped_missing`` row first for every symbol lacking a bar on ``day``.

    Earlier ``dropped_missing`` rows (from the composite) are replaced by the final one;
    other records are kept in order. Symbols that do have the bar keep only non-dropped rows.
    """
    start = day - timedelta(days=c.config.data.backfill_calendar_days)
    rows: list[SymbolQuality] = []
    for symbol in universe:
        if symbol in present:
            continue
        reasons = [
            q.detail for q in quality if q.symbol == symbol and q.status == "dropped_missing"
        ]
        detail = f"no bar for {day.isoformat()}"
        if reasons:
            detail = f"{detail}; {reasons[-1]}"
        rows.append(
            SymbolQuality(
                symbol=symbol,
                status="dropped_missing",
                detail=detail,
                bars_available=c.bars.days_available(symbol, start, day),
                source="",
            )
        )
    rows.extend(q for q in quality if q.status != "dropped_missing")
    return tuple(rows)


# ----------------------------------------------------------------------------- analyze


def _read_portfolio(c: Container) -> PortfolioContext:
    """Read account and positions, capturing provider failures in ``error``."""
    try:
        account = c.account.account()
        positions = tuple(c.account.positions())
    except DataSourceError as exc:
        logger.warning("account read failed: %s", exc)
        return PortfolioContext(account=None, positions=(), error=str(exc))
    return PortfolioContext(account=account, positions=positions, error=None)


def analyze_job(c: Container, as_of: AsOf, quality_in: Sequence[SymbolQuality]) -> AnalysisResult:
    """Analyse stored bars up to ``as_of.day`` and persist the run and portfolio snapshot.

    Parameters
    ----------
    c : Container
        Dependencies.
    as_of : AsOf
        As-of to analyse (typically :attr:`FetchOutcome.as_of`).
    quality_in : Sequence[SymbolQuality]
        Fetch-time quality records merged into the report.

    Returns
    -------
    AnalysisResult
        The persisted analysis.

    Raises
    ------
    AnalysisError
        If the analysis cannot be completed (e.g. too few symbols).
    """
    universe = c.config.universe.symbols
    start = as_of.day - timedelta(days=c.config.data.backfill_calendar_days)
    bars = c.bars.load(universe, start, as_of.day)
    result = run_analysis(
        bars, universe, as_of, c.config.analysis, c.clock.now(), quality_in=quality_in
    )
    c.runs.save_analysis(result)
    c.runs.save_portfolio(result.run_id, _read_portfolio(c))
    logger.info(
        "analysis %s for %s: %d of %d symbols analysed",
        result.run_id,
        as_of.day,
        len(result.quality.analysed),
        len(universe),
    )
    return result


# ----------------------------------------------------------------------------- report


def idempotency_key(run_id: str, recipients: Sequence[str]) -> str:
    """Return ``"<run_id>-<first 8 hex of sha1(comma-joined recipients)>"``.

    Parameters
    ----------
    run_id : str
        Analysis run.
    recipients : Sequence[str]
        Email recipients.

    Returns
    -------
    str
        Stable key used by Resend's ``Idempotency-Key`` and the outbox file name.
    """
    digest = hashlib.sha1(",".join(recipients).encode(), usedforsecurity=False).hexdigest()
    return f"{run_id}-{digest[:8]}"


def _previous_largest(c: Container, run_id: str) -> tuple[str, ...] | None:
    """Return the largest set of the run preceding ``run_id`` (None if no such run)."""
    prev = c.runs.previous_run_id(run_id)
    if prev is None:
        return None
    largest = c.runs.load_analysis(prev).largest_set
    return largest.symbols if largest is not None else ()


def report_job(c: Container, run_id: str, out_dir: Path | None = None) -> ReportBundle:
    """Build the email (brief + workbook) for a stored run.

    Parameters
    ----------
    c : Container
        Dependencies.
    run_id : str
        Stored analysis run.
    out_dir : Path or None
        When given, the workbook is also written there.

    Returns
    -------
    ReportBundle
        Result, portfolio, ready-to-send message and the Sheets URL (None when Sheets is
        disabled or publishing failed).

    Raises
    ------
    StoreError
        If the run does not exist.
    ReportError
        If rendering fails or the workbook cannot be written to ``out_dir``.
    """
    result = c.runs.load_analysis(run_id)
    try:
        portfolio = c.runs.load_portfolio(run_id)
    except StoreError as exc:
        logger.warning("no portfolio snapshot for %s: %s", run_id, exc)
        portfolio = PortfolioContext(account=None, positions=(), error="no account snapshot")
    inputs = BriefInputs(
        result=result,
        portfolio=portfolio,
        previous_largest=_previous_largest(c, run_id),
        report_cfg=c.config.report,
        app_version=__version__,
    )
    try:
        subject = subject_line(inputs)
        text = render_text(inputs)
        html = render_html(inputs)
        workbook = build_workbook(inputs)
    except Exception as exc:  # any rendering failure is a report failure (exit code 5)
        raise ReportError(f"rendering run {run_id} failed: {exc}") from exc
    filename = workbook_filename(result.as_of.day)

    sheets_url: str | None = None
    if c.sheets is not None:
        try:
            sheets_url = c.sheets.publish(inputs)
            logger.info("published run %s to Google Sheets: %s", run_id, sheets_url)
        except ReportError as exc:
            logger.error("Google Sheets publish failed: %s", exc)

    if out_dir is not None:
        path = out_dir / filename
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(workbook)
        except OSError as exc:
            raise ReportError(f"cannot write workbook {path}: {exc}") from exc
        logger.info("wrote workbook %s", path)

    recipients = c.settings.email_to
    message = EmailMessage(
        sender=c.settings.email_from,
        recipients=recipients,
        subject=subject,
        text_body=text,
        html_body=html,
        attachments=(Attachment(filename=filename, content=workbook, mime_type=XLSX_MIME),),
        idempotency_key=idempotency_key(run_id, recipients),
    )
    return ReportBundle(result=result, portfolio=portfolio, message=message, sheets_url=sheets_url)


# ----------------------------------------------------------------------------- send


def send_job(c: Container, bundle: ReportBundle, force: bool = False) -> SendReceipt:
    """Send the report email once per run and record the delivery.

    In a dry run (``c.dry_run``) the message goes to the file outbox, repeats are allowed and
    nothing is written to ``email_log``.

    Parameters
    ----------
    c : Container
        Dependencies.
    bundle : ReportBundle
        Report built by :func:`report_job`.
    force : bool
        Send even if an email was already logged for this run.

    Returns
    -------
    SendReceipt
        Backend confirmation.

    Raises
    ------
    EmailSendError
        If the run was already sent (``retryable=False``) or the backend fails.
    """
    run_id = bundle.result.run_id
    if not c.dry_run and not force and c.runs.email_sent_for(run_id):
        raise EmailSendError(
            c.sender.name, f"already sent for run {run_id} (use --force)", retryable=False
        )
    receipt = c.sender.send(bundle.message)
    if c.dry_run:
        logger.info("dry run: wrote %s (not recorded as sent)", receipt.message_id)
    else:
        c.runs.log_email(run_id, receipt, bundle.message.recipients)
        logger.info("sent run %s via %s (%s)", run_id, receipt.backend, receipt.message_id)
    return receipt


# ----------------------------------------------------------------------------- daily


def daily_job(c: Container, as_of_override: date | None = None) -> SendReceipt:
    """Run fetch, analyze, report and send for one as-of.

    Parameters
    ----------
    c : Container
        Dependencies (``c.dry_run`` selects the file outbox).
    as_of_override : date or None
        Explicit as-of date; resolved from the clock when None.

    Returns
    -------
    SendReceipt
        Receipt of the sent (or dry-run) email.
    """
    as_of = resolve_run_as_of(c, as_of_override)
    logger.info("daily run as of %s (%s): %s", as_of.day, as_of.mode, as_of.note)
    fetched = fetch_job(c, as_of)
    result = analyze_job(c, fetched.as_of, fetched.quality)
    bundle = report_job(c, result.run_id)
    return send_job(c, bundle)
