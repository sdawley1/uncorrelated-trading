"""NYSE trading calendar and as-of resolution.

Every command and the scheduler call :func:`resolve_as_of` to decide which trading day's close
is the latest one to include. Calendars return :class:`~uncorrelated_trading.models.TradingDay`
rows whose ``open_utc``/``close_utc`` carry the session times, so early closes (13:00 ET) need
no special cases downstream.

Two calendars are provided:

* :class:`StaticCalendar` computes NYSE holidays and early closes by rule (see
  https://www.nyse.com/markets/hours-calendars). It cannot know ad hoc closures (for example
  national days of mourning), so it is the fallback.
* :class:`CachedCalendar` serves rows cached in a :class:`CalendarStore` (the SQLite
  ``calendar_days`` table), refreshing them from the Alpaca calendar when older than
  ``max_age_days`` (remote attempts at least a day apart), and falls back to
  :class:`StaticCalendar` when Alpaca is unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.models import AsOf, AsOfMode, TradingDay

logger = logging.getLogger(__name__)

CalendarSource = Literal["alpaca", "static"]

_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)
_LOOKBACK_DAYS = 10
_PREVIOUS_DAY_MAX_STEPS = 40  # 40 windows of 10 days: more than a year of lookback
_COVERAGE_MARGIN_DAYS = 10  # longer than any gap between consecutive NYSE sessions
_REMOTE_RETRY_SPACING = timedelta(days=1)  # minimum spacing of remote calendar attempts


# ----------------------------------------------------------------------------- protocols


class TradingCalendar(Protocol):
    """Anything that can list trading sessions in a date range."""

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return the trading sessions with ``start <= day <= end``, ascending."""
        ...


class CalendarStore(Protocol):
    """Persistence needed by :class:`CachedCalendar` (satisfied by ``BarStore``)."""

    def upsert_trading_days(self, days: Iterable[TradingDay]) -> None:
        """Insert or replace calendar rows."""
        ...

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return cached sessions with ``start <= day <= end``, ascending."""
        ...

    def calendar_last_refreshed(self) -> datetime | None:
        """Return when the cache was last refreshed from the remote, or None."""
        ...

    def set_calendar_refreshed(self, when: datetime) -> None:
        """Record the instant of a successful remote refresh."""
        ...


# ----------------------------------------------------------------------------- holiday rules


def _easter(year: int) -> date:
    """Return Western Easter Sunday via the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th (1-based) ``weekday`` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last ``weekday`` (Mon=0) of a month."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Shift a fixed-date holiday: Saturday to Friday, Sunday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> frozenset[date]:
    """Return the rule-based NYSE full-day holidays observed in ``year``.

    Rules (https://www.nyse.com/markets/hours-calendars): New Year's Day (Sunday observed
    Monday; a Saturday New Year's is not observed on the preceding Friday), Martin Luther
    King Jr. Day (3rd Monday of January), Presidents' Day (3rd Monday of February), Good
    Friday (Easter minus two days), Memorial Day (last Monday of May), Juneteenth (June 19,
    observed, from 2022), Independence Day (July 4, observed), Labor Day (1st Monday of
    September), Thanksgiving (4th Thursday of November) and Christmas (December 25, observed).
    Saturday holidays are observed on Friday and Sunday holidays on Monday.

    Parameters
    ----------
    year : int
        Calendar year.

    Returns
    -------
    frozenset[date]
        Weekday dates within ``year`` on which the exchange is closed. Ad hoc closures are
        not included.
    """
    days: set[date] = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        days.add(_observed(new_year))
    days.add(_nth_weekday(year, 1, 0, 3))
    days.add(_nth_weekday(year, 2, 0, 3))
    days.add(_easter(year) - timedelta(days=2))
    days.add(_last_weekday(year, 5, 0))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    days.add(_observed(date(year, 7, 4)))
    days.add(_nth_weekday(year, 9, 0, 1))
    days.add(_nth_weekday(year, 11, 3, 4))
    days.add(_observed(date(year, 12, 25)))
    return frozenset(days)


def nyse_early_closes(year: int) -> frozenset[date]:
    """Return the rule-based NYSE 13:00 ET early-close days in ``year``.

    Early closes (https://www.nyse.com/markets/hours-calendars) are the day after
    Thanksgiving, and July 3 and December 24 when they are weekdays and not holidays.

    Parameters
    ----------
    year : int
        Calendar year.

    Returns
    -------
    frozenset[date]
        Trading days in ``year`` whose session closes at 13:00 exchange-local time.
    """
    holidays = nyse_holidays(year)
    days = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    for candidate in (date(year, 7, 3), date(year, 12, 24)):
        if candidate.weekday() < 5 and candidate not in holidays:
            days.add(candidate)
    return frozenset(days)


# ----------------------------------------------------------------------------- calendars


class StaticCalendar:
    """Weekdays minus rule-computed NYSE holidays; 09:30-16:00 sessions, 13:00 early closes."""

    def __init__(self, tz: str = "America/New_York") -> None:
        """Create a calendar whose sessions are in exchange time zone ``tz``.

        Parameters
        ----------
        tz : str
            IANA time zone of the exchange.
        """
        self._tz = ZoneInfo(tz)

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return trading sessions with ``start <= day <= end``, ascending.

        Parameters
        ----------
        start, end : date
            Inclusive exchange-local date range; an empty list when ``start > end``.

        Returns
        -------
        list[TradingDay]
            Sessions with open and close converted to UTC.
        """
        out: list[TradingDay] = []
        holidays: dict[int, frozenset[date]] = {}
        early: dict[int, frozenset[date]] = {}
        n_days = (end - start).days + 1
        for i in range(max(n_days, 0)):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            if d.year not in holidays:
                holidays[d.year] = nyse_holidays(d.year)
                early[d.year] = nyse_early_closes(d.year)
            if d in holidays[d.year]:
                continue
            close = _EARLY_CLOSE if d in early[d.year] else _SESSION_CLOSE
            out.append(
                TradingDay(
                    day=d,
                    open_utc=datetime.combine(d, _SESSION_OPEN, self._tz).astimezone(UTC),
                    close_utc=datetime.combine(d, close, self._tz).astimezone(UTC),
                )
            )
        return out


class CachedCalendar:
    """Alpaca-backed calendar cached in a :class:`CalendarStore`; falls back to StaticCalendar.

    The cache is refreshed with ``fetch_remote(today - 400 d, today + 60 d)`` when it has never
    been refreshed, is older than ``max_age_days``, or does not cover the requested range.
    Remote attempts (successful or not) are spaced at least one day apart, so a failing remote
    is not hammered while a long-running process (``trader run``) still refreshes. When the
    remote is missing or fails and the cache does not cover the range, the range is served
    from :class:`StaticCalendar` (``source = "static"``, logged as a warning); a stale cache
    that still covers the range keeps being used.
    """

    source: CalendarSource

    def __init__(
        self,
        store: CalendarStore,
        fetch_remote: Callable[[date, date], list[TradingDay]] | None,
        clock: Clock,
        tz: str,
        max_age_days: int = 30,
    ) -> None:
        """Create the calendar.

        Parameters
        ----------
        store : CalendarStore
            Persistent cache (``BarStore`` in production).
        fetch_remote : Callable[[date, date], list[TradingDay]] or None
            Remote calendar lookup over an inclusive date range (Alpaca ``get_calendar``);
            None disables remote refresh.
        clock : Clock
            Source of the current instant.
        tz : str
            IANA exchange time zone (used to compute "today" and for the static fallback).
        max_age_days : int
            Maximum cache age before a refresh is attempted.
        """
        self._store = store
        self._fetch_remote = fetch_remote
        self._clock = clock
        self._tz = ZoneInfo(tz)
        self._static = StaticCalendar(tz)
        self._max_age = timedelta(days=max_age_days)
        self._last_attempt: datetime | None = None
        self.source = "static"

    def _covers(self, start: date, end: date) -> bool:
        """Return True when cached rows bracket ``[start, end]``."""
        margin = timedelta(days=_COVERAGE_MARGIN_DAYS)
        rows = self._store.trading_days(start - margin, end + margin)
        return bool(rows) and rows[0].day <= start and rows[-1].day >= end

    def _is_stale(self) -> bool:
        """Return True when the cache was never refreshed or is older than the max age."""
        last = self._store.calendar_last_refreshed()
        return last is None or self._clock.now() - last > self._max_age

    def _may_attempt(self) -> bool:
        """Return True when no remote attempt was made in the last day."""
        last = self._last_attempt
        return last is None or self._clock.now() - last >= _REMOTE_RETRY_SPACING

    def _refresh(self) -> bool:
        """Fetch the remote calendar once and upsert it; return True on success."""
        self._last_attempt = self._clock.now()
        if self._fetch_remote is None:
            return False
        today = self._clock.now().astimezone(self._tz).date()
        try:
            rows = self._fetch_remote(today - timedelta(days=400), today + timedelta(days=60))
        except Exception as exc:  # any remote failure means "use the fallback"
            logger.warning("calendar refresh failed: %s", exc)
            return False
        self._store.upsert_trading_days(rows)
        self._store.set_calendar_refreshed(self._clock.now())
        return True

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """Return trading sessions with ``start <= day <= end``, ascending.

        Parameters
        ----------
        start, end : date
            Inclusive exchange-local date range.

        Returns
        -------
        list[TradingDay]
            Cached Alpaca sessions, or rule-based sessions when the cache cannot serve the
            range. ``self.source`` records which; read it after the query.
        """
        covered = self._covers(start, end)
        if self._may_attempt() and (not covered or self._is_stale()):
            self._refresh()
            covered = self._covers(start, end)
        if covered:
            self.source = "alpaca"
            return self._store.trading_days(start, end)
        logger.warning(
            "calendar cache does not cover %s..%s; using static NYSE calendar", start, end
        )
        self.source = "static"
        return self._static.trading_days(start, end)


# ----------------------------------------------------------------------------- as-of


def _require_aware(now_utc: datetime) -> datetime:
    """Return ``now_utc`` in UTC, rejecting naive datetimes."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        msg = "now_utc must be tz-aware"
        raise ValueError(msg)
    return now_utc.astimezone(UTC)


def previous_trading_day(day: date, calendar: TradingCalendar) -> date:
    """Return the last trading day strictly before ``day``.

    Parameters
    ----------
    day : date
        Reference exchange-local date.
    calendar : TradingCalendar
        Calendar to consult.

    Returns
    -------
    date
        The latest session date earlier than ``day``.

    Raises
    ------
    ValueError
        If no trading day is found within roughly a year before ``day``.
    """
    end = day - timedelta(days=1)
    for _ in range(_PREVIOUS_DAY_MAX_STEPS):
        start = end - timedelta(days=_LOOKBACK_DAYS - 1)
        rows = calendar.trading_days(start, end)
        if rows:
            return rows[-1].day
        end = start - timedelta(days=1)
    msg = f"no trading day found before {day.isoformat()}"
    raise ValueError(msg)


def resolve_as_of(
    now_utc: datetime,
    calendar: TradingCalendar,
    tz: str,
    settle_minutes: int,
    calendar_source: CalendarSource = "static",
) -> AsOf:
    """Resolve which trading day's close is the latest one a run should include.

    With ``today`` the exchange-local date of ``now_utc``:

    * ``today`` not a trading day: ``non_trading_day``, previous trading day;
    * before the open: ``pre_open``, previous trading day;
    * between open and close: ``intraday``, previous trading day (today excluded);
    * within ``settle_minutes`` after the close: ``post_close_settling``, today, provisional;
    * later: ``post_close``, today.

    Session times come from the calendar rows, so early closes are handled automatically.

    Parameters
    ----------
    now_utc : datetime
        Current instant (tz-aware).
    calendar : TradingCalendar
        Calendar to consult.
    tz : str
        IANA exchange time zone.
    settle_minutes : int
        Minutes after the close during which today's bars are treated as provisional.
    calendar_source : {"alpaca", "static"}
        Recorded on the result for the Data Quality section.

    Returns
    -------
    AsOf
        The resolved as-of day, mode and explanatory note.

    Raises
    ------
    ValueError
        If ``now_utc`` is naive.
    """
    now = _require_aware(now_utc)
    today = now.astimezone(ZoneInfo(tz)).date()
    rows = calendar.trading_days(today - timedelta(days=_LOOKBACK_DAYS), today + timedelta(days=1))
    session = next((r for r in rows if r.day == today), None)
    earlier = [r.day for r in rows if r.day < today]
    prev = earlier[-1] if earlier else previous_trading_day(today, calendar)

    mode: AsOfMode
    if session is None:
        mode, day = "non_trading_day", prev
        note = f"{today.isoformat()} is not a trading day; using last session {prev.isoformat()}"
    elif now < session.open_utc:
        mode, day = "pre_open", prev
        note = f"before today's open; using previous session {prev.isoformat()}"
    elif now < session.close_utc:
        mode, day = "intraday", prev
        note = (
            f"market open; today's session ({today.isoformat()}) is excluded, "
            f"using previous session {prev.isoformat()}"
        )
    elif now < session.close_utc + timedelta(minutes=settle_minutes):
        mode, day = "post_close_settling", today
        note = (
            f"within {settle_minutes} min of today's close; today's bars are provisional "
            "and may still be revised"
        )
    else:
        mode, day = "post_close", today
        note = "after today's close; using today's session"
    return AsOf(
        day=day,
        mode=mode,
        now_utc=now,
        provisional=mode == "post_close_settling",
        calendar_source=calendar_source,
        note=note,
    )


def snap_as_of(
    requested: date,
    calendar: TradingCalendar,
    now_utc: datetime,
    tz: str = "America/New_York",
    settle_minutes: int = 20,
) -> AsOf:
    """Build the :class:`AsOf` for an explicit ``--as-of`` override.

    ``today`` is the exchange-local date of ``now_utc``. A date after ``today`` is rejected.
    ``requested == today`` resolves exactly like a run without override
    (:func:`resolve_as_of`: ``pre_open``/``intraday`` use the previous session,
    ``post_close_settling`` is provisional), so an in-progress session is never stored or
    analysed as a close. An earlier trading day is used as is (``mode="post_close"``); an
    earlier non-trading day is snapped back to the previous trading day
    (``mode="non_trading_day"``) and a warning is logged. The calendar source is read from
    ``calendar.source`` when present, else ``"static"``.

    Parameters
    ----------
    requested : date
        Date given on the command line.
    calendar : TradingCalendar
        Calendar to consult.
    now_utc : datetime
        Current instant (tz-aware), recorded on the result.
    tz : str
        IANA exchange time zone defining ``today``.
    settle_minutes : int
        Settle window passed to :func:`resolve_as_of` when ``requested`` is today.

    Returns
    -------
    AsOf
        The override as-of; provisional only for today within the settle window.

    Raises
    ------
    ValueError
        If ``now_utc`` is naive or ``requested`` is after today in the exchange time zone.
    """
    now = _require_aware(now_utc)
    today = now.astimezone(ZoneInfo(tz)).date()
    if requested > today:
        msg = f"--as-of {requested.isoformat()} is in the future (today is {today.isoformat()})"
        raise ValueError(msg)
    if requested == today:
        calendar.trading_days(today - timedelta(days=_LOOKBACK_DAYS), today)
        raw = getattr(calendar, "source", "static")
        resolved = resolve_as_of(
            now,
            calendar,
            tz,
            settle_minutes,
            calendar_source="alpaca" if raw == "alpaca" else "static",
        )
        note = f"--as-of override {requested.isoformat()} is today; {resolved.note}"
        return replace(resolved, note=note)
    rows = calendar.trading_days(requested, requested)
    raw_source = getattr(calendar, "source", "static")
    source: CalendarSource = "alpaca" if raw_source == "alpaca" else "static"
    if rows:
        return AsOf(
            day=requested,
            mode="post_close",
            now_utc=now,
            provisional=False,
            calendar_source=source,
            note=f"--as-of override {requested.isoformat()}",
        )
    day = previous_trading_day(requested, calendar)
    note = (
        f"--as-of override {requested.isoformat()} is not a trading day; "
        f"snapped back to {day.isoformat()}"
    )
    logger.warning(note)
    return AsOf(
        day=day,
        mode="non_trading_day",
        now_utc=now,
        provisional=False,
        calendar_source=source,
        note=note,
    )


def data_request_end(
    as_of: AsOf, tz: str, now_utc: datetime, sip_guard_minutes: int = 16
) -> datetime:
    """Return the ``end`` instant for market-data requests.

    ``end = min(23:59:59 of as_of.day in the exchange zone, now_utc - sip_guard_minutes)``, so
    the Alpaca SIP 15-minute restriction is never hit.

    Parameters
    ----------
    as_of : AsOf
        Resolved as-of.
    tz : str
        IANA exchange time zone.
    now_utc : datetime
        Current instant (tz-aware).
    sip_guard_minutes : int
        Minutes to stay behind ``now_utc``.

    Returns
    -------
    datetime
        Tz-aware UTC instant.
    """
    now = _require_aware(now_utc)
    day_end = datetime.combine(as_of.day, time(23, 59, 59), ZoneInfo(tz)).astimezone(UTC)
    return min(day_end, now - timedelta(minutes=sip_guard_minutes))
