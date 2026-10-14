from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from factories import sample_analysis_result, sample_portfolio
from uncorrelated_trading.app import Container
from uncorrelated_trading.calendar import StaticCalendar
from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.config import AppConfig, Settings
from uncorrelated_trading.data.composite import CompositeDataSource
from uncorrelated_trading.errors import DataSourceError, EmailSendError
from uncorrelated_trading.jobs import (
    analyze_job,
    daily_job,
    fetch_job,
    report_job,
    resolve_run_as_of,
    send_job,
)
from uncorrelated_trading.models import (
    AccountSnapshot,
    AsOf,
    Bar,
    EmailMessage,
    PositionSnapshot,
    ReportBundle,
    SendReceipt,
    TradingDay,
)
from uncorrelated_trading.store.bars import BarStore
from uncorrelated_trading.store.db import connect, migrate
from uncorrelated_trading.store.runs import RunStore

SYMBOLS = ("S01", "S02", "S03", "S04", "S05", "S06")
AS_OF_DAY = date(2026, 10, 13)
POST_CLOSE_NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)  # 17:30 ET
SETTLING_NOW = datetime(2026, 10, 13, 20, 5, tzinfo=UTC)  # 16:05 ET, inside the settle window
CAL = StaticCalendar()
NY = ZoneInfo("America/New_York")


# ----------------------------------------------------------------------------- fakes


def trading_days(end: date, n: int) -> list[date]:
    rows = CAL.trading_days(end - timedelta(days=2 * n + 30), end)
    return [r.day for r in rows][-n:]


def close_for(symbol: str, day: date) -> float:
    i = int(symbol[1:])
    t = day.toordinal()
    return 100.0 * math.exp(0.01 * math.sin(t * (0.37 + 0.11 * i)) + 0.0005 * (t % 97) * (i % 3))


def make_bar(symbol: str, day: date, close: float | None = None, source: str = "fake") -> Bar:
    c = close_for(symbol, day) if close is None else close
    return Bar(symbol, day, c, c * 1.01, c * 0.99, c, 1_000_000, source)


@dataclass
class FakeSource:
    """Market data source serving synthetic bars on trading days up to a per-symbol last day."""

    published_through: date
    last_day: dict[str, date] = field(default_factory=dict)
    overrides: dict[tuple[str, date], float] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], date, datetime]] = field(default_factory=list)
    name: str = "fake"

    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]:
        self.calls.append((tuple(symbols), start, end_utc))
        end_day = min(end_utc.astimezone(NY).date(), self.published_through)
        out: list[Bar] = []
        for row in CAL.trading_days(start, end_day):
            for s in symbols:
                if row.day <= self.last_day.get(s, end_day):
                    out.append(make_bar(s, row.day, self.overrides.get((s, row.day))))
        return out


@dataclass
class FakeAccount:
    paper: bool = True
    fail: bool = False

    def account(self) -> AccountSnapshot:
        if self.fail:
            raise DataSourceError("alpaca", "401 unauthorized", retryable=False)
        return AccountSnapshot(POST_CLOSE_NOW, 1000.0, 5000.0, 2000.0, 5000.0, 4900.0, "USD", True)

    def positions(self) -> list[PositionSnapshot]:
        if self.fail:
            raise DataSourceError("alpaca", "401 unauthorized", retryable=False)
        return [PositionSnapshot("S01", 3.0, 90.0, 100.0, 300.0, 270.0, 30.0, 0.11, 0.01)]

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        return CAL.trading_days(start, end)


@dataclass
class FakeSender:
    fail: bool = False
    sent: list[EmailMessage] = field(default_factory=list)
    name: str = "fake"

    def send(self, message: EmailMessage) -> SendReceipt:
        if self.fail:
            raise EmailSendError("fake", "535 auth failed", retryable=False)
        self.sent.append(message)
        return SendReceipt("fake", f"id-{len(self.sent)}", POST_CLOSE_NOW)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "t.sqlite3")
    migrate(c)
    yield c
    c.close()


def make_container(
    tmp_path: Path,
    conn: sqlite3.Connection,
    source: FakeSource,
    *,
    now: datetime = POST_CLOSE_NOW,
    account: FakeAccount | None = None,
    sender: FakeSender | None = None,
    dry_run: bool = False,
) -> Container:
    cfg_path = tmp_path / "app.toml"
    symbols = ", ".join(f'"{s}"' for s in SYMBOLS)
    cfg_path.write_text(
        f"[universe]\nsymbols = [{symbols}]\n"
        "[analysis]\nwindows = [60, 120]\nprimary_window = 60\nsearch_time_budget_s = 2.0\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(
        {
            "UT_DATA_DIR": str(tmp_path),
            "EMAIL_BACKEND": "file",
            "EMAIL_FROM": "bot@example.com",
            "EMAIL_TO": "a@example.com,b@example.com",
        },
        dotenv_path=None,
        require_alpaca=False,
    )
    clock = FakeClock(now)
    config = AppConfig.load(cfg_path)
    return Container(
        settings=settings,
        config=config,
        clock=clock,
        conn=conn,
        bars=BarStore(conn, clock),
        runs=RunStore(conn, clock),
        calendar=CAL,
        data_source=CompositeDataSource(
            primary=source,
            fallback=None,
            tolerance=config.data.close_mismatch_tolerance,
            fallback_max=0,
            crosscheck_fraction=0.0,
            min_budget_for_crosscheck=100,
            budget=None,
            sleep=clock.sleep,
        ),
        account=account or FakeAccount(),
        sender=sender or FakeSender(),
        sheets=None,
        dry_run=dry_run,
    )


def post_close(day: date = AS_OF_DAY) -> AsOf:
    return AsOf(day, "post_close", POST_CLOSE_NOW, False, "static", "after today's close")


def seed(bars: BarStore, through: date, n: int = 300, symbols: Sequence[str] = SYMBOLS) -> None:
    bars.upsert(make_bar(s, d) for s in symbols for d in trading_days(through, n))


# ----------------------------------------------------------------------------- fetch


def test_fetch_job_incremental_requests_overlap(tmp_path: Path, conn: sqlite3.Connection):
    source = FakeSource(published_through=AS_OF_DAY)
    c = make_container(tmp_path, conn, source)
    seed(c.bars, date(2026, 10, 9), n=30)

    out = fetch_job(c, post_close())

    assert len(source.calls) == 1
    symbols, start, _ = source.calls[0]
    assert set(symbols) == set(SYMBOLS)
    assert start == date(2026, 10, 9) - timedelta(days=7)
    assert out.as_of.day == AS_OF_DAY
    assert not out.fell_back_to_previous_day
    assert out.quality == ()
    assert all(c.bars.last_day(s) == AS_OF_DAY for s in SYMBOLS)
    assert out.bars_written == len(SYMBOLS) * len(CAL.trading_days(start, AS_OF_DAY))


def test_fetch_job_backfills_empty_store_and_groups_by_start(
    tmp_path: Path, conn: sqlite3.Connection
):
    source = FakeSource(published_through=AS_OF_DAY)
    c = make_container(tmp_path, conn, source)
    seed(c.bars, date(2026, 10, 9), n=30, symbols=SYMBOLS[:2])

    fetch_job(c, post_close())

    starts = {start: set(symbols) for symbols, start, _ in source.calls}
    assert starts == {
        date(2026, 10, 2): set(SYMBOLS[:2]),
        AS_OF_DAY - timedelta(days=420): set(SYMBOLS[2:]),
    }


def test_fetch_end_respects_sip_rule(tmp_path: Path, conn: sqlite3.Connection):
    source = FakeSource(published_through=AS_OF_DAY)
    c = make_container(tmp_path, conn, source, now=SETTLING_NOW)
    as_of = resolve_run_as_of(c, None)
    assert as_of.mode == "post_close_settling"
    assert as_of.day == AS_OF_DAY

    fetch_job(c, as_of)

    for _, _, end_utc in source.calls:
        assert end_utc <= SETTLING_NOW - timedelta(minutes=16)
        assert end_utc.tzinfo is not None


def test_fetch_falls_back_when_today_bars_missing(tmp_path: Path, conn: sqlite3.Connection):
    source = FakeSource(published_through=date(2026, 10, 12))
    c = make_container(tmp_path, conn, source, now=SETTLING_NOW)
    as_of = resolve_run_as_of(c, None)
    assert as_of.mode == "post_close_settling"

    out = fetch_job(c, as_of)

    assert out.fell_back_to_previous_day
    assert out.as_of.day == date(2026, 10, 12)
    assert out.as_of.mode == "post_close_settling"
    assert "not yet published" in out.as_of.note
    assert not any(q.status == "dropped_missing" for q in out.quality)


def test_fetch_falls_back_in_post_close_mode(tmp_path: Path, conn: sqlite3.Connection):
    source = FakeSource(published_through=date(2026, 10, 12))
    c = make_container(tmp_path, conn, source)

    out = fetch_job(c, post_close())  # every bar for today is missing

    assert out.fell_back_to_previous_day
    assert out.as_of.day == date(2026, 10, 12)


@pytest.mark.parametrize("mode", ["intraday", "pre_open", "non_trading_day"])
def test_fetch_falls_back_in_any_mode(tmp_path: Path, conn: sqlite3.Connection, mode: str):
    # Behaviour changed in the final fix wave (C1): the fallback now applies to every mode.
    source = FakeSource(published_through=date(2026, 10, 9))
    c = make_container(tmp_path, conn, source)
    as_of = AsOf(date(2026, 10, 12), mode, POST_CLOSE_NOW, False, "static", "prior note")  # type: ignore[arg-type]

    out = fetch_job(c, as_of)

    assert out.fell_back_to_previous_day
    assert out.as_of.day == date(2026, 10, 9)
    assert out.as_of.mode == mode
    assert "bars for 2026-10-12 unavailable; using previous session" in out.as_of.note
    assert out.as_of.note.startswith("prior note; ")
    assert not any(q.status == "dropped_missing" for q in out.quality)


def test_fetch_does_not_fall_back_when_most_bars_present(tmp_path: Path, conn: sqlite3.Connection):
    halted = date(2026, 10, 7)
    source = FakeSource(published_through=AS_OF_DAY, last_day={"S06": halted})
    c = make_container(tmp_path, conn, source)

    out = fetch_job(c, post_close())

    assert not out.fell_back_to_previous_day
    assert out.as_of.day == AS_OF_DAY
    dropped = [q for q in out.quality if q.status == "dropped_missing"]
    assert [q.symbol for q in dropped] == ["S06"]
    assert dropped[0].bars_available > 0
    # the missing symbol was retried once with its own request
    assert any(symbols == ("S06",) for symbols, _, _ in source.calls)


def test_fetch_detects_restated_close_and_refetches(tmp_path: Path, conn: sqlite3.Connection):
    restated_day = date(2026, 10, 8)
    source = FakeSource(published_through=AS_OF_DAY)
    c = make_container(tmp_path, conn, source)
    seed(c.bars, date(2026, 10, 9), n=30)
    source.overrides[("S03", restated_day)] = close_for("S03", restated_day) * 1.05

    out = fetch_job(c, post_close())

    mismatch = [q for q in out.quality if q.status == "close_mismatch"]
    assert [q.symbol for q in mismatch] == ["S03"]
    assert "restated" in mismatch[0].detail
    assert restated_day.isoformat() in mismatch[0].detail
    full = [call for call in source.calls if call[0] == ("S03",)]
    assert len(full) == 1
    assert full[0][1] == AS_OF_DAY - timedelta(days=420)
    stored = c.bars.load(["S03"], restated_day, restated_day)
    assert stored[0].close == pytest.approx(close_for("S03", restated_day) * 1.05)


def test_resolve_run_as_of_override_snaps_weekend(tmp_path: Path, conn: sqlite3.Connection):
    c = make_container(tmp_path, conn, FakeSource(published_through=AS_OF_DAY))
    as_of = resolve_run_as_of(c, date(2026, 10, 11))  # Sunday
    assert as_of.day == date(2026, 10, 9)
    assert as_of.mode == "non_trading_day"


# ----------------------------------------------------------------------------- analyze


def test_analyze_job_persists_and_captures_account_error(tmp_path: Path, conn: sqlite3.Connection):
    account = FakeAccount(fail=True)
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY), account=account)
    seed(c.bars, AS_OF_DAY)

    result = analyze_job(c, post_close(), ())

    loaded = c.runs.load_analysis(result.run_id)
    assert loaded.as_of.day == AS_OF_DAY
    assert set(loaded.quality.analysed) == set(SYMBOLS)
    portfolio = c.runs.load_portfolio(result.run_id)
    assert portfolio.account is None
    assert portfolio.positions == ()
    assert portfolio.error is not None
    assert "401" in portfolio.error


def test_analyze_job_stores_account_and_positions(tmp_path: Path, conn: sqlite3.Connection):
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY))
    seed(c.bars, AS_OF_DAY)

    result = analyze_job(c, post_close(), ())

    portfolio = c.runs.load_portfolio(result.run_id)
    assert portfolio.error is None
    assert portfolio.account is not None
    assert [p.symbol for p in portfolio.positions] == ["S01"]


# ----------------------------------------------------------------------------- report / send


def test_report_job_builds_message_and_writes_workbook(tmp_path: Path, conn: sqlite3.Connection):
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY))
    seed(c.bars, AS_OF_DAY)
    first = analyze_job(c, post_close(), ())
    c.clock.advance(60)  # type: ignore[attr-defined]
    second = analyze_job(c, post_close(), ())
    out_dir = tmp_path / "reports"

    bundle = report_job(c, second.run_id, out_dir)

    assert bundle.result.run_id == second.run_id
    assert first.run_id != second.run_id
    msg = bundle.message
    assert msg.recipients == ("a@example.com", "b@example.com")
    assert msg.sender == "bot@example.com"
    assert msg.idempotency_key.startswith(f"{second.run_id}-")
    assert len(msg.idempotency_key) == len(second.run_id) + 9
    assert msg.subject
    assert msg.text_body
    assert "<" in msg.html_body
    (attachment,) = msg.attachments
    assert attachment.filename == "uncorrelated_2026-10-13.xlsx"
    assert (out_dir / attachment.filename).read_bytes() == attachment.content
    assert bundle.sheets_url is None


def _bundle(message_key: str = "run-key") -> ReportBundle:
    result = sample_analysis_result()
    message = EmailMessage(
        "bot@example.com", ("a@example.com",), "subj", "text", "<p>html</p>", (), message_key
    )
    return ReportBundle(result, sample_portfolio(), message, None)


def test_send_job_refuses_duplicate(tmp_path: Path, conn: sqlite3.Connection):
    sender = FakeSender()
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY), sender=sender)
    bundle = _bundle()

    receipt = send_job(c, bundle)
    assert receipt.message_id == "id-1"
    assert c.runs.email_sent_for(bundle.result.run_id)

    with pytest.raises(EmailSendError) as info:
        send_job(c, bundle)
    assert info.value.retryable is False
    assert len(sender.sent) == 1

    forced = send_job(c, bundle, force=True)
    assert forced.message_id == "id-2"


def test_send_job_failure_does_not_log(tmp_path: Path, conn: sqlite3.Connection):
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY), sender=FakeSender(fail=True))
    bundle = _bundle()
    with pytest.raises(EmailSendError):
        send_job(c, bundle)
    assert not c.runs.email_sent_for(bundle.result.run_id)


def test_dry_run_send_is_not_logged_as_sent(tmp_path: Path, conn: sqlite3.Connection):
    sender = FakeSender()
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY), sender=sender, dry_run=True)
    bundle = _bundle()
    send_job(c, bundle)
    send_job(c, bundle)
    assert len(sender.sent) == 2
    assert not c.runs.email_sent_for(bundle.result.run_id)


def test_daily_job_end_to_end_with_fakes(tmp_path: Path, conn: sqlite3.Connection):
    sender = FakeSender()
    source = FakeSource(published_through=AS_OF_DAY)
    c = make_container(tmp_path, conn, source, sender=sender)

    receipt = daily_job(c)

    assert receipt.backend == "fake"
    (message,) = sender.sent
    run_id = c.runs.latest_run_id()
    assert run_id is not None
    assert message.idempotency_key.startswith(run_id)
    assert c.runs.email_sent_for(run_id)
    assert c.runs.load_analysis(run_id).as_of.day == AS_OF_DAY


def test_report_job_previous_largest_from_earlier_run(tmp_path: Path, conn: sqlite3.Connection):
    c = make_container(tmp_path, conn, FakeSource(AS_OF_DAY))
    seed(c.bars, AS_OF_DAY)
    first = analyze_job(c, post_close(), ())
    c.clock.advance(60)  # type: ignore[attr-defined]
    second = analyze_job(c, replace(post_close(), note="second"), ())
    first_bundle = report_job(c, first.run_id)
    second_bundle = report_job(c, second.run_id)
    assert "no previous run" in first_bundle.message.text_body
    assert "Changes since last run: largest set unchanged" in second_bundle.message.text_body
