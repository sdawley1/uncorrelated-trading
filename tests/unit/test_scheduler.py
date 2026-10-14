from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from factories import sample_analysis_result, sample_portfolio
from uncorrelated_trading import jobs
from uncorrelated_trading.calendar import StaticCalendar
from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.config import ScheduleConfig
from uncorrelated_trading.errors import DataSourceError, EmailSendError, StoreError
from uncorrelated_trading.models import AsOf, EmailMessage, ReportBundle, SendReceipt
from uncorrelated_trading.scheduler import Job, Scheduler, build_jobs, daily_at
from uncorrelated_trading.store.db import connect, migrate
from uncorrelated_trading.store.runs import RunStore

TZ = "America/New_York"
NY = ZoneInfo(TZ)
CAL = StaticCalendar()


def ny(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


def every_day(d: date) -> bool:
    return True


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "t.sqlite3")
    migrate(c)
    yield c
    c.close()


@dataclass
class Recorder:
    calls: list[date] = field(default_factory=list)
    fail: bool = False

    def __call__(self, d: date) -> None:
        self.calls.append(d)
        if self.fail:
            raise RuntimeError("boom")


def job(
    action: Callable[[date], None],
    *,
    at: time = time(7, 0),
    name: str = "send",
    grace_h: int = 3,
    startup: bool = False,
) -> Job:
    return Job(name, daily_at(at, TZ, every_day), action, timedelta(hours=grace_h), startup)


def job_row(conn: sqlite3.Connection, name: str, d: date) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE job = ? AND run_date = ?",
        (name, d.isoformat()),
    ).fetchone()
    return None if row is None else (row["status"], row["detail"])


# ----------------------------------------------------------------------------- daily_at


def test_daily_at_crosses_dst():
    nxt = daily_at(time(7, 0), TZ, every_day)
    first = nxt(ny(2026, 3, 7, 12))
    second = nxt(first)
    assert first == datetime(2026, 3, 8, 11, 0, tzinfo=UTC)  # 07:00 EDT (UTC-4)
    assert first.astimezone(NY).date() == date(2026, 3, 8)
    assert second.astimezone(NY).date() == date(2026, 3, 9)
    assert second - first == timedelta(hours=24)
    # 2026-03-08 is the spring-forward day: its 07:00 is 23 h after 07:00 EST on 03-07.
    before = nxt(ny(2026, 3, 6, 12))
    assert before == datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # 07:00 EST (UTC-5)
    assert first - before == timedelta(hours=23)


def test_daily_at_strictly_after_now_and_respects_allowed():
    nxt = daily_at(time(7, 0), TZ, lambda d: d.weekday() < 5)
    # Exactly at the fire time: the next one is returned.
    assert nxt(ny(2026, 10, 14, 7)) == ny(2026, 10, 15, 7)
    # Friday after 07:00 -> Monday.
    assert nxt(ny(2026, 10, 16, 8)) == ny(2026, 10, 19, 7)


def test_daily_at_raises_when_never_allowed():
    nxt = daily_at(time(7, 0), TZ, lambda d: False)
    with pytest.raises(ValueError, match="no allowed date"):
        nxt(ny(2026, 10, 14, 8))


# ----------------------------------------------------------------------------- tick


def test_tick_runs_job_once_per_date(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 59))
    action = Recorder()
    s = Scheduler([job(action)], clock, RunStore(conn, clock), TZ, 30)
    assert s.tick() == []
    clock.advance(90)  # 07:00:30
    assert s.tick() == ["send"]
    clock.advance(60)
    assert s.tick() == []
    clock.advance(3600)
    assert s.tick() == []
    assert action.calls == [date(2026, 10, 14)]
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")
    clock.advance(24 * 3600)  # next day 08:01:30
    assert s.tick() == ["send"]
    assert action.calls == [date(2026, 10, 14), date(2026, 10, 15)]


def test_restart_does_not_resend(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 50))
    first = Recorder()
    s1 = Scheduler([job(first, startup=True)], clock, RunStore(conn, clock), TZ, 30)
    clock.advance(11 * 60)  # 07:01
    assert s1.tick() == ["send"]
    assert first.calls == [date(2026, 10, 14)]

    # Process restarts at 07:30 (within the 3 h grace) over the same database.
    clock.advance(29 * 60)
    second = Recorder()
    s2 = Scheduler([job(second, startup=True)], clock, RunStore(conn, clock), TZ, 30)
    assert s2.tick() == []
    clock.advance(3600)
    assert s2.tick() == []
    assert second.calls == []
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")


def test_startup_catches_up_missed_send(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 8, 30))
    action = Recorder()
    s = Scheduler([job(action, startup=True)], clock, RunStore(conn, clock), TZ, 30)
    assert s.tick() == ["send"]
    assert action.calls == [date(2026, 10, 14)]


def test_startup_without_catch_up_waits_for_next_day(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 8, 30))
    action = Recorder()
    s = Scheduler([job(action, startup=False)], clock, RunStore(conn, clock), TZ, 30)
    assert s.tick() == []
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


def test_misfire_within_grace_runs(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 0))
    action = Recorder()
    s = Scheduler([job(action)], clock, RunStore(conn, clock), TZ, 30)
    clock.advance(int(3.5 * 3600))  # laptop slept until 09:30, 2.5 h late
    assert s.tick() == ["send"]
    assert action.calls == [date(2026, 10, 14)]


def test_misfire_beyond_grace_skips_and_logs(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
):
    clock = FakeClock(ny(2026, 10, 14, 6, 0))
    action = Recorder()
    s = Scheduler([job(action)], clock, RunStore(conn, clock), TZ, 30)
    clock.advance(5 * 3600)  # 11:00, 4 h late
    with caplog.at_level(logging.WARNING, logger="uncorrelated_trading.scheduler"):
        assert s.tick() == []
    assert action.calls == []
    assert any("missed" in r.getMessage() for r in caplog.records)
    status = job_row(conn, "send", date(2026, 10, 14))
    assert status is not None and status[0] == "missed"
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


def test_sleep_across_days_runs_latest_within_grace(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 13, 6, 0))
    action = Recorder()
    s = Scheduler([job(action)], clock, RunStore(conn, clock), TZ, 30)
    clock.advance(26 * 3600)  # asleep until 08:00 the next day
    assert s.tick() == ["send"]
    assert action.calls == [date(2026, 10, 14)]
    status = job_row(conn, "send", date(2026, 10, 13))
    assert status is not None and status[0] == "missed"


def test_exception_in_job_does_not_stop_loop(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
):
    clock = FakeClock(ny(2026, 10, 14, 6, 59))
    bad = Recorder(fail=True)
    good = Recorder()
    s = Scheduler(
        [job(bad, name="refresh"), job(good, name="send")], clock, RunStore(conn, clock), TZ, 30
    )
    clock.advance(120)
    with caplog.at_level(logging.ERROR, logger="uncorrelated_trading.scheduler"):
        ran = s.tick()
    assert ran == ["refresh", "send"]
    assert good.calls == [date(2026, 10, 14)]
    status = job_row(conn, "refresh", date(2026, 10, 14))
    assert status is not None and status[0] == "error" and "boom" in status[1]
    assert any(r.exc_info for r in caplog.records)
    clock.advance(24 * 3600)
    assert s.tick() == ["refresh", "send"]
    assert len(bad.calls) == 2


def test_stop_event_ends_run_forever(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 0))
    stop = threading.Event()
    s = Scheduler([job(Recorder())], clock, RunStore(conn, clock), TZ, 1, stop_event=stop)
    t = threading.Thread(target=s.run_forever, daemon=True)
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_sigterm_sets_stop_event_and_restores_handler(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 7, 1))
    before = signal.getsignal(signal.SIGTERM)

    def terminate(d: date) -> None:
        signal.raise_signal(signal.SIGTERM)

    s = Scheduler([job(terminate, startup=True)], clock, RunStore(conn, clock), TZ, 60)
    s.run_forever()
    assert s.stop_event.is_set()
    assert signal.getsignal(signal.SIGTERM) is before


def test_tick_error_does_not_end_run_forever(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 0))
    stop = threading.Event()
    s = Scheduler([job(Recorder())], clock, RunStore(conn, clock), TZ, 0, stop_event=stop)
    ticks = {"n": 0}

    def flaky() -> list[str]:
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("database is locked")
        stop.set()
        return []

    s.tick = flaky  # type: ignore[method-assign]
    s.run_forever()
    assert ticks["n"] == 2


def test_stop_event_preset_returns_immediately(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 0))
    stop = threading.Event()
    stop.set()
    action = Recorder()
    Scheduler([job(action)], clock, RunStore(conn, clock), TZ, 1, stop_event=stop).run_forever()
    assert action.calls == []


# ----------------------------------------------------------------------------- retry


@dataclass
class FlakySender:
    """Sender whose first ``failures`` sends raise a retryable SMTP-style error."""

    clock: FakeClock
    failures: int = 1
    attempts: int = 0
    name: str = "smtp"

    def send(self, message: EmailMessage) -> SendReceipt:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise EmailSendError("smtp", "421 service not available", retryable=True)
        return SendReceipt("smtp", f"id-{self.attempts}", self.clock.now())


def send_setup(
    conn: sqlite3.Connection, clock: FakeClock, failures: int
) -> tuple[FlakySender, Callable[[date], None], RunStore]:
    runs = RunStore(conn, clock)
    result = sample_analysis_result()
    runs.save_analysis(result)
    sender = FlakySender(clock, failures)
    c: Any = SimpleNamespace(dry_run=False, runs=runs, sender=sender)
    message = EmailMessage("bot@example.com", ("a@example.com",), "s", "t", "<p>h</p>", (), "k")
    bundle = ReportBundle(result, sample_portfolio(), message, None)

    def action(d: date) -> None:
        jobs.send_job(c, bundle)

    return sender, action, runs


def email_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0])


def test_retryable_send_failure_is_retried_after_backoff(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 59))
    sender, action, runs = send_setup(conn, clock, failures=1)
    s = Scheduler([job(action)], clock, runs, TZ, 30, retry_backoff_seconds=300)
    clock.advance(120)  # 07:01
    assert s.tick() == ["send"]
    status = job_row(conn, "send", date(2026, 10, 14))
    assert status is not None and status[0] == "retry" and "421" in status[1]
    assert s.next_fire_times() == {"send": ny(2026, 10, 14, 7)}
    clock.advance(60)  # still inside the backoff
    assert s.tick() == []
    clock.advance(300)
    assert s.tick() == ["send"]
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")
    assert sender.attempts == 2
    assert email_rows(conn) == 1
    clock.advance(3600)
    assert s.tick() == []
    assert email_rows(conn) == 1
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


@pytest.mark.parametrize(
    "exc",
    [
        DataSourceError("alpaca", "503", retryable=True),
        StoreError("database is locked"),
        OSError("network is unreachable"),
    ],
)
def test_other_retryable_errors_are_retried(conn: sqlite3.Connection, exc: Exception):
    clock = FakeClock(ny(2026, 10, 14, 7, 1))
    calls: list[date] = []

    def action(d: date) -> None:
        calls.append(d)
        if len(calls) == 1:
            raise exc

    s = Scheduler([job(action, startup=True)], clock, RunStore(conn, clock), TZ, 30)
    assert s.tick() == ["send"]
    assert job_row(conn, "send", date(2026, 10, 14))[0] == "retry"  # type: ignore[index]
    clock.advance(301)
    assert s.tick() == ["send"]
    assert calls == [date(2026, 10, 14)] * 2
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")


def test_non_retryable_error_is_final(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 7, 1))
    calls: list[date] = []

    def action(d: date) -> None:
        calls.append(d)
        raise EmailSendError("smtp", "535 auth failed", retryable=False)

    s = Scheduler([job(action, startup=True)], clock, RunStore(conn, clock), TZ, 30)
    assert s.tick() == ["send"]
    clock.advance(3600)
    assert s.tick() == []
    assert len(calls) == 1
    assert job_row(conn, "send", date(2026, 10, 14))[0] == "error"  # type: ignore[index]


def test_retries_stop_at_the_grace(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 6, 59))
    sender, action, runs = send_setup(conn, clock, failures=100)
    s = Scheduler([job(action, grace_h=1)], clock, runs, TZ, 30, retry_backoff_seconds=1200)
    for _ in range(12):
        clock.advance(600)
        s.tick()
    # Attempts at 07:09, 07:29 and 07:49; a fourth (08:09) would be outside the 1 h grace,
    # so the third failure is recorded as a final error.
    assert sender.attempts == 3
    assert job_row(conn, "send", date(2026, 10, 14))[0] == "error"  # type: ignore[index]
    assert email_rows(conn) == 0
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


def leave_running(conn: sqlite3.Connection, started: datetime) -> None:
    """Simulate a crash mid-send: the claim row stays ``running``."""
    conn.execute(
        "INSERT INTO job_runs (job, run_date, status, started_at) VALUES (?, ?, 'running', ?)",
        ("send", "2026-10-14", started.astimezone(UTC).isoformat(timespec="microseconds")),
    )
    conn.commit()


def test_crashed_running_row_is_reclaimed_within_grace(conn: sqlite3.Connection):
    # Crash at 07:00, restart at 07:20: the 20-minute-old claim is stale (15 min default)
    # while the fire is still within the 3 h grace, so the email still goes out once.
    leave_running(conn, ny(2026, 10, 14, 7))
    clock = FakeClock(ny(2026, 10, 14, 7, 20))
    sender, action, runs = send_setup(conn, clock, failures=0)
    s = Scheduler([job(action, startup=True)], clock, runs, TZ, 30)
    assert s.tick() == ["send"]
    assert sender.attempts == 1
    assert email_rows(conn) == 1
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")
    clock.advance(3600)
    assert s.tick() == []
    assert email_rows(conn) == 1


def test_restart_right_after_crash_waits_then_reclaims(conn: sqlite3.Connection):
    # Crash with the claim at 07:00, restart one minute later (systemd RestartSec=60), tick
    # every 30 s until 10:00: the fresh claim is not stolen, but the fire is not skipped
    # either; it runs once the claim is 15 minutes old.
    leave_running(conn, ny(2026, 10, 14, 7))
    clock = FakeClock(ny(2026, 10, 14, 7, 1))
    sender, action, runs = send_setup(conn, clock, failures=0)
    ran_at: list[datetime] = []

    def timed(d: date) -> None:
        ran_at.append(clock.now())
        action(d)

    s = Scheduler([job(timed, startup=True)], clock, runs, TZ, 30)
    for _ in range(int(3 * 3600 / 30)):
        s.tick()
        clock.advance(30)
    assert len(ran_at) == 1
    assert ran_at[0] >= ny(2026, 10, 14, 7, 15)
    assert ran_at[0] <= ny(2026, 10, 14, 7, 16)
    assert email_rows(conn) == 1
    assert job_row(conn, "send", date(2026, 10, 14)) == ("ok", "")
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


def test_running_claim_outlasting_the_grace_is_missed(conn: sqlite3.Connection):
    leave_running(conn, ny(2026, 10, 14, 7, 50))
    clock = FakeClock(ny(2026, 10, 14, 7, 55))
    sender, action, runs = send_setup(conn, clock, failures=0)
    s = Scheduler([job(action, startup=True, grace_h=1)], clock, runs, TZ, 30)
    assert s.tick() == []  # reclaimable only at 08:05, after the 08:00 end of grace
    assert sender.attempts == 0
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


def test_finished_rows_still_skip_and_advance(conn: sqlite3.Connection):
    clock = FakeClock(ny(2026, 10, 14, 7, 1))
    runs = RunStore(conn, clock)
    runs.job_finished("send", date(2026, 10, 14), "error", "boom")
    calls: list[date] = []
    s = Scheduler([job(calls.append, startup=True)], clock, runs, TZ, 30)
    assert s.tick() == []
    assert calls == []
    assert s.next_fire_times() == {"send": ny(2026, 10, 15, 7)}


# ----------------------------------------------------------------------------- build_jobs


def fake_container(
    conn: sqlite3.Connection, clock: FakeClock, send_on_days: str = "weekdays"
) -> Any:
    schedule = ScheduleConfig(time(16, 25), time(7, 0), send_on_days, 30, 3, 6)  # type: ignore[arg-type]
    return SimpleNamespace(
        config=SimpleNamespace(schedule=schedule),
        settings=SimpleNamespace(timezone=TZ),
        clock=clock,
        runs=RunStore(conn, clock),
        calendar=CAL,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []
    as_of = AsOf(date(2026, 10, 14), "post_close", ny(2026, 10, 14, 17), False, "static", "test")
    monkeypatch.setattr(jobs, "resolve_run_as_of", lambda c, o: as_of)
    monkeypatch.setattr(
        jobs,
        "fetch_job",
        lambda c, a: (
            log.append("fetch")
            or SimpleNamespace(as_of=a, quality=(), primary_failures=0, fallback_calls=0)
        ),
    )
    monkeypatch.setattr(
        jobs,
        "analyze_job",
        lambda c, a, q, **kw: log.append("analyze") or SimpleNamespace(run_id="r1"),
    )

    def report(c: Any, r: str, publish_sheets: bool = True) -> str:
        log.append(f"report:{r}" + ("" if publish_sheets else ":no-sheets"))
        return "bundle"

    monkeypatch.setattr(jobs, "report_job", report)
    monkeypatch.setattr(jobs, "send_job", lambda c, b: log.append(f"send:{b}"))
    monkeypatch.setattr(jobs, "daily_job", lambda c: log.append("daily"))
    return log


def test_build_jobs_table(conn: sqlite3.Connection, calls: list[str]):
    clock = FakeClock(ny(2026, 10, 14, 12))
    built = {j.name: j for j in build_jobs(fake_container(conn, clock))}
    assert set(built) == {"refresh", "send"}
    refresh, send = built["refresh"], built["send"]
    assert refresh.misfire_grace == timedelta(hours=6)
    assert not refresh.run_on_startup_if_missed
    assert send.misfire_grace == timedelta(hours=3)
    assert send.run_on_startup_if_missed
    assert refresh.next_fire(clock.now()) == ny(2026, 10, 14, 16, 25)
    assert send.next_fire(clock.now()) == ny(2026, 10, 15, 7)
    # Friday evening: the next refresh is Monday (trading days only).
    assert refresh.next_fire(ny(2026, 10, 16, 17)) == ny(2026, 10, 19, 16, 25)
    # 2026-11-26 is Thanksgiving: no refresh that day.
    assert refresh.next_fire(ny(2026, 11, 25, 17)) == ny(2026, 11, 27, 16, 25)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("weekdays", ny(2026, 11, 26, 7)),  # Thanksgiving is a weekday
        ("trading_days", ny(2026, 11, 27, 7)),
        ("every_day", ny(2026, 11, 26, 7)),
    ],
)
def test_send_on_days(conn: sqlite3.Connection, calls: list[str], mode: str, expected: datetime):
    clock = FakeClock(ny(2026, 11, 25, 12))
    send = {j.name: j for j in build_jobs(fake_container(conn, clock, mode))}["send"]
    assert send.next_fire(clock.now()) == expected
    saturday = send.next_fire(ny(2026, 11, 27, 8))
    assert saturday == (ny(2026, 11, 28, 7) if mode == "every_day" else ny(2026, 11, 30, 7))


def test_refresh_action_fetches_analyses_and_reports(conn: sqlite3.Connection, calls: list[str]):
    clock = FakeClock(ny(2026, 10, 14, 16, 30))
    refresh = {j.name: j for j in build_jobs(fake_container(conn, clock))}["refresh"]
    refresh.action(date(2026, 10, 14))
    assert calls == ["fetch", "analyze", "report:r1:no-sheets"]  # Sheets published by send


def test_send_action_without_runs_runs_daily(conn: sqlite3.Connection, calls: list[str]):
    clock = FakeClock(ny(2026, 10, 14, 7))
    send = {j.name: j for j in build_jobs(fake_container(conn, clock))}["send"]
    send.action(date(2026, 10, 14))
    assert calls == ["daily"]


def test_send_action_sends_latest_unsent_or_runs_daily(
    conn: sqlite3.Connection, calls: list[str], monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock(ny(2026, 10, 14, 7))
    c = fake_container(conn, clock)
    sent = {"flag": False}
    monkeypatch.setattr(c.runs, "latest_run_id", lambda: "r9")
    monkeypatch.setattr(c.runs, "email_sent_for", lambda r: sent["flag"])
    send = {j.name: j for j in build_jobs(c)}["send"]
    send.action(date(2026, 10, 14))
    assert calls == ["report:r9", "send:bundle"]
    sent["flag"] = True
    calls.clear()
    send.action(date(2026, 10, 14))
    assert calls == ["daily"]
