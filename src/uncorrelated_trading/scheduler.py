"""Stdlib scheduler for ``trader run`` with idempotent daily jobs (spec A10).

A :class:`Job` knows its next fire instant (:func:`daily_at` builds the exchange-local wall
time with :mod:`zoneinfo` for each date and converts it to UTC, so DST changes never shift a
job), its action, a misfire grace and whether a missed fire is caught up at startup.

:class:`Scheduler` polls the wall clock: :meth:`Scheduler.tick` runs every job whose fire
instant has passed, provided it is still within the grace (otherwise the miss is logged and
recorded). Each ``(job, local date)`` is claimed through
:meth:`~uncorrelated_trading.store.runs.RunStore.job_started` before running, so a job fires at
most once per local date, and a restart never sends the same morning email twice. A job that
fails with a retryable error (``DataSourceError``/``EmailSendError`` with ``retryable=True``,
``StoreError`` or ``OSError``) is recorded as ``retry`` and run again after
``retry_backoff_seconds`` while the original fire instant is still within the grace; any other
failure is recorded as a final ``error``. A ``running`` row older than the grace (a crash
mid-job) is reclaimed.
:meth:`Scheduler.run_forever` waits on a :class:`threading.Event` for at most ``poll_seconds``
between ticks and exits cleanly on SIGINT/SIGTERM.

:func:`build_jobs` returns the two production jobs: ``refresh`` (fetch, analyse and build the
report after the close on trading days) and ``send`` (email the latest unsent report, running
the full daily job first when there is none).
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from uncorrelated_trading import jobs
from uncorrelated_trading.app import Container
from uncorrelated_trading.clock import Clock
from uncorrelated_trading.errors import DataSourceError, EmailSendError, StoreError
from uncorrelated_trading.store.runs import RunStore

logger = logging.getLogger(__name__)

_MAX_SEARCH_DAYS = 400
_MAX_CATCH_UP_FIRES = 400
DEFAULT_RETRY_BACKOFF_SECONDS = 300


def is_retryable(exc: BaseException) -> bool:
    """Return True when a job failure is transient and worth retrying within the grace.

    Parameters
    ----------
    exc : BaseException
        Exception raised by a job action.

    Returns
    -------
    bool
        ``exc.retryable`` for :class:`DataSourceError` and :class:`EmailSendError`; True for
        :class:`StoreError` and :class:`OSError`; False otherwise.
    """
    if isinstance(exc, DataSourceError | EmailSendError):
        return bool(exc.retryable)
    return isinstance(exc, StoreError | OSError)


# ----------------------------------------------------------------------------- jobs


@dataclass(frozen=True, slots=True)
class Job:
    """A scheduled job.

    Attributes
    ----------
    name : str
        Job name; also the ``job_runs`` key.
    next_fire : Callable[[datetime], datetime]
        Given the current UTC instant, the next fire instant (UTC), strictly later.
    action : Callable[[date], None]
        Work to do; receives the exchange-local date of the fire instant.
    misfire_grace : timedelta
        How late a fire may still run (e.g. after laptop sleep).
    run_on_startup_if_missed : bool
        Whether a fire that passed shortly before startup (within the grace) is caught up.
    """

    name: str
    next_fire: Callable[[datetime], datetime]
    action: Callable[[date], None]
    misfire_grace: timedelta
    run_on_startup_if_missed: bool


def daily_at(
    local_time: time, tz: str, allowed: Callable[[date], bool]
) -> Callable[[datetime], datetime]:
    """Build a ``next_fire`` function for a daily wall-clock time.

    Parameters
    ----------
    local_time : time
        Naive wall-clock time in ``tz``.
    tz : str
        IANA time zone of the wall clock.
    allowed : Callable[[date], bool]
        Predicate selecting the local dates on which the job fires.

    Returns
    -------
    Callable[[datetime], datetime]
        Function mapping a tz-aware ``now`` to the first instant strictly after it whose local
        wall time is ``local_time`` on an allowed date, in UTC. Each candidate is built with
        ``datetime.combine(day, local_time, ZoneInfo(tz))`` (never by adding 24 h), so DST is
        respected; a wall time skipped by a spring-forward gap resolves with ``fold=0``
        (i.e. the equivalent instant just after the gap) on that same date. The function
        raises ``ValueError`` when no allowed date occurs within 400 days.
    """
    zone = ZoneInfo(tz)
    utc = ZoneInfo("UTC")

    def next_fire(now_utc: datetime) -> datetime:
        if now_utc.tzinfo is None:
            msg = "now_utc must be tz-aware"
            raise ValueError(msg)
        day = now_utc.astimezone(zone).date()
        for _ in range(_MAX_SEARCH_DAYS):
            if allowed(day):
                candidate = datetime.combine(day, local_time, zone).astimezone(utc)
                if candidate > now_utc:
                    return candidate
            day += timedelta(days=1)
        msg = f"no allowed date for {local_time} {tz} within {_MAX_SEARCH_DAYS} days"
        raise ValueError(msg)

    return next_fire


# ----------------------------------------------------------------------------- scheduler


class Scheduler:
    """Poll-based scheduler running each job at most once per exchange-local date.

    Parameters
    ----------
    jobs : Sequence[Job]
        Jobs to schedule; ticks run them in this order.
    clock : Clock
        Wall clock (a ``FakeClock`` in tests).
    runs : RunStore
        Store holding the ``job_runs`` idempotency table.
    tz : str
        Exchange time zone; a fire's run date is its local date in this zone.
    poll_seconds : int
        Maximum wait between ticks in :meth:`run_forever`.
    stop_event : threading.Event or None
        Event ending :meth:`run_forever`; a new one when None.
    retry_backoff_seconds : int
        Wait before re-running a job that failed with a retryable error.
    """

    def __init__(
        self,
        jobs: Sequence[Job],
        clock: Clock,
        runs: RunStore,
        tz: str,
        poll_seconds: int,
        stop_event: threading.Event | None = None,
        retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        """Create the scheduler and compute each job's first due instant."""
        self._jobs = tuple(jobs)
        self._backoff = timedelta(seconds=retry_backoff_seconds)
        self._retry_at: dict[str, datetime] = {}
        self._clock = clock
        self._runs = runs
        self._zone = ZoneInfo(tz)
        self._poll_seconds = poll_seconds
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        now = clock.now()
        self._due: dict[str, datetime] = {job.name: self._first_due(job, now) for job in jobs}

    @staticmethod
    def _first_due(job: Job, now: datetime) -> datetime:
        """Return the first due instant, catching up a recent miss when the job asks for it."""
        if job.run_on_startup_if_missed:
            recent = job.next_fire(now - job.misfire_grace)
            if recent <= now:
                # Already-run dates are filtered by job_started() in tick().
                return recent
        return job.next_fire(now)

    def next_fire_times(self) -> dict[str, datetime]:
        """Return each job's pending due instant (UTC).

        Returns
        -------
        dict[str, datetime]
            Job name to the instant it next fires (or the missed instant awaiting catch-up).
        """
        return dict(self._due)

    def _local_date(self, when: datetime) -> date:
        """Return the exchange-local date of ``when``."""
        return when.astimezone(self._zone).date()

    def _run(self, job: Job, run_date: date, due: datetime, now: datetime) -> bool:
        """Run ``job`` for ``run_date`` and record its outcome; never raises job errors.

        Returns True when the job failed retryably and another attempt after the backoff
        still falls within the grace of ``due`` (status ``retry``); a failure that cannot be
        retried is recorded as ``error``.
        """
        logger.info("running job %s for %s", job.name, run_date)
        try:
            job.action(run_date)
        except Exception as exc:
            retry = is_retryable(exc) and now + self._backoff - due <= job.misfire_grace
            if retry:
                logger.warning(
                    "job %s for %s failed (%s); retrying in %s",
                    job.name,
                    run_date,
                    exc,
                    self._backoff,
                )
            else:
                logger.exception("job %s for %s failed", job.name, run_date)
            self._runs.job_finished(job.name, run_date, "retry" if retry else "error", repr(exc))
            return retry
        self._runs.job_finished(job.name, run_date, "ok", "")
        logger.info("job %s for %s finished", job.name, run_date)
        return False

    def _handle_due(self, job: Job, due: datetime, now: datetime) -> tuple[bool, bool]:
        """Process one passed fire instant; return (action invoked, retry pending)."""
        run_date = self._local_date(due)
        late = now - due
        if late > job.misfire_grace:
            logger.warning(
                "job %s for %s missed: %s late exceeds grace %s",
                job.name,
                run_date,
                late,
                job.misfire_grace,
            )
            if self._runs.job_started(job.name, run_date, job.misfire_grace):
                detail = f"missed by {late} (grace {job.misfire_grace})"
                self._runs.job_finished(job.name, run_date, "missed", detail)
            return False, False
        if not self._runs.job_started(job.name, run_date, job.misfire_grace):
            logger.info("job %s for %s already ran; skipping", job.name, run_date)
            return False, False
        return True, self._run(job, run_date, due, now)

    def tick(self) -> list[str]:
        """Run every job whose due instant has passed, once.

        A fire within its grace runs (if no ``job_runs`` row exists for its local date yet,
        or the row awaits a retry); a later one is logged as missed. After a retryable failure
        the job's due instant is kept and the job is skipped until the backoff has passed.
        Fires skipped while the process was suspended are each processed in order, so after a
        long sleep the most recent fire still runs if it is within the grace. Job exceptions
        are logged and recorded, never propagated.

        Returns
        -------
        list[str]
            Names of the jobs whose action was invoked (including ones that raised).
        """
        now = self._clock.now()
        ran: list[str] = []
        for job in self._jobs:
            retry_at = self._retry_at.get(job.name)
            if retry_at is not None and now < retry_at:
                continue
            self._retry_at.pop(job.name, None)
            due = self._due[job.name]
            for _ in range(_MAX_CATCH_UP_FIRES):
                if due > now:
                    break
                invoked, retry = self._handle_due(job, due, now)
                if invoked and job.name not in ran:
                    ran.append(job.name)
                if retry:
                    self._retry_at[job.name] = now + self._backoff
                    break
                due = job.next_fire(due)
            else:
                due = job.next_fire(now)
            self._due[job.name] = due
        return ran

    def run_forever(self) -> None:
        """Tick until ``stop_event`` is set, waiting at most ``poll_seconds`` between ticks.

        SIGINT and SIGTERM set ``stop_event`` (handlers are installed only when called from
        the main thread and restored on exit). The wall clock is re-read on every tick, so a
        suspended laptop catches up on wake within each job's grace.
        """
        previous: dict[signal.Signals, Callable[[int, FrameType | None], Any] | int] = {}

        def _stop(signum: int, frame: FrameType | None) -> None:
            logger.info("received signal %d; stopping scheduler", signum)
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old = signal.signal(sig, _stop)
                if old is not None:
                    previous[sig] = old
            except ValueError:  # not the main thread
                pass
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:  # e.g. the database is briefly unavailable; retry next poll
                    logger.exception("scheduler tick failed")
                self.stop_event.wait(self._poll_seconds)
        finally:
            for restore, handler in previous.items():
                signal.signal(restore, handler)
            logger.info("scheduler stopped")


# ----------------------------------------------------------------------------- job table


def _send_days(c: Container) -> Callable[[date], bool]:
    """Return the ``allowed`` predicate for ``schedule.send_on_days``."""
    mode = c.config.schedule.send_on_days
    if mode == "weekdays":
        return lambda d: d.weekday() < 5
    if mode == "trading_days":
        return lambda d: bool(c.calendar.trading_days(d, d))
    return lambda d: True


def build_jobs(c: Container) -> list[Job]:
    """Build the production job table (spec A10).

    Parameters
    ----------
    c : Container
        Dependencies; ``c.config.schedule`` supplies times, days and graces and
        ``c.settings.timezone`` the exchange time zone.

    Returns
    -------
    list[Job]
        ``refresh`` at ``refresh_time`` on trading days (fetch, analyse, build the report;
        no send, no Sheets publish) and ``send`` at ``send_time`` on ``send_on_days`` (send
        the latest run if it has not been emailed, else run the full daily job), with
        startup catch-up for ``send``.
    """
    schedule = c.config.schedule
    tz = c.settings.timezone

    def trading_day(d: date) -> bool:
        return bool(c.calendar.trading_days(d, d))

    def refresh(run_date: date) -> None:
        as_of = jobs.resolve_run_as_of(c, None)
        fetched = jobs.fetch_job(c, as_of)
        result = jobs.analyze_job(
            c,
            fetched.as_of,
            fetched.quality,
            primary_source_failures=fetched.primary_failures,
            fallback_calls=fetched.fallback_calls,
        )
        jobs.report_job(c, result.run_id, publish_sheets=False)

    def send(run_date: date) -> None:
        run_id = c.runs.latest_run_id()
        if run_id is not None and not c.runs.email_sent_for(run_id):
            jobs.send_job(c, jobs.report_job(c, run_id))
            return
        jobs.daily_job(c)

    return [
        Job(
            "refresh",
            daily_at(schedule.refresh_time, tz, trading_day),
            refresh,
            timedelta(hours=schedule.refresh_misfire_grace_hours),
            run_on_startup_if_missed=False,
        ),
        Job(
            "send",
            daily_at(schedule.send_time, tz, _send_days(c)),
            send,
            timedelta(hours=schedule.send_misfire_grace_hours),
            run_on_startup_if_missed=True,
        ),
    ]
