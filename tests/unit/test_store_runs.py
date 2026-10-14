import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import StoreError
from uncorrelated_trading.models import (
    AccountSnapshot,
    AnalysisParams,
    AnalysisResult,
    AsOf,
    CorrelationMatrix,
    DataQualityReport,
    MomentumScore,
    PortfolioContext,
    PositionSnapshot,
    SendReceipt,
    SymbolQuality,
    UncorrelatedSet,
    correlation_matrices_equal,
)
from uncorrelated_trading.store.db import connect, migrate
from uncorrelated_trading.store.runs import RunStore

NOW = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)


def _uset(symbols: tuple[str, ...], rho: float, *, spearman_ok: bool = True) -> UncorrelatedSet:
    return UncorrelatedSet(
        symbols=symbols,
        size=len(symbols),
        max_abs_rho=rho,
        mean_abs_rho=rho / 2,
        max_abs_rho_ci_upper=rho + 0.1,
        min_eigenvalue=0.8,
        spearman_max_abs_rho=rho + 0.02,
        passes_spearman=spearman_ok,
        stable_in_long_window=not spearman_ok,
        mean_momentum_z=0.25,
    )


def make_result(run_id: str = "a" * 32, created_at: datetime = NOW) -> AnalysisResult:
    symbols = ("AAA", "BBB", "CCC")
    pearson = np.array([[1.0, 0.1, -0.05], [0.1, 1.0, 0.15], [-0.05, 0.15, 1.0]])
    spearman = np.array([[1.0, 0.12, -0.04], [0.12, 1.0, 0.18], [-0.04, 0.18, 1.0]])
    corr = tuple(
        CorrelationMatrix(
            window=60,
            method=method,
            symbols=symbols,
            values=values,
            n_obs=60,
            start_day=date(2026, 7, 17),
            end_day=date(2026, 10, 13),
        )
        for method, values in (("pearson", pearson), ("spearman", spearman))
    )
    triplet = _uset(symbols, 0.15, spearman_ok=False)
    return AnalysisResult(
        run_id=run_id,
        as_of=AsOf(
            day=date(2026, 10, 13),
            mode="post_close",
            now_utc=NOW,
            provisional=False,
            calendar_source="alpaca",
            note="after settle",
        ),
        created_at=created_at,
        params=AnalysisParams(
            primary_window=60,
            windows=(60, 120, 250),
            tau=0.2,
            tau_spearman=0.3,
            max_set_size_exact=4,
            max_sets_per_size=50,
            search_time_budget_s=20.0,
            momentum_k=20,
            momentum_long=120,
            momentum_skip=20,
            momentum_weights=(0.4, 0.3, 0.3),
            direction_threshold=0.5,
        ),
        universe=symbols,
        correlations=corr,
        pairs=(_uset(("AAA", "CCC"), 0.05), _uset(("AAA", "BBB"), 0.1)),
        sets_by_size={3: (triplet,)},
        largest_set=triplet,
        largest_set_exact=True,
        momentum=(
            MomentumScore("BBB", 0.05, 0.08, 1.2, 0.25, 1.1, 1, "up"),
            MomentumScore("AAA", 0.01, 0.02, 0.3, 0.22, 0.1, 2, "flat"),
            MomentumScore("CCC", -0.04, -0.06, -1.0, 0.3, -1.2, 3, "down"),
        ),
        quality=DataQualityReport(
            requested=("AAA", "BBB", "CCC", "DDD"),
            analysed=symbols,
            per_symbol=(
                SymbolQuality("AAA", "ok", "", 250, "alpaca"),
                SymbolQuality("BBB", "fallback_used", "alpaca empty", 250, "twelvedata"),
                SymbolQuality("CCC", "ok", "", 250, "alpaca"),
                SymbolQuality("DDD", "dropped_missing", "bars stop 2026-10-09", 248, "alpaca"),
            ),
            primary_source_failures=1,
            fallback_calls=1,
        ),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> RunStore:
    conn = connect(tmp_path / "db.sqlite3")
    migrate(conn)
    return RunStore(conn, clock=clock)


def assert_results_equal(a: AnalysisResult, b: AnalysisResult) -> None:
    assert a.run_id == b.run_id
    assert a.as_of == b.as_of
    assert a.created_at == b.created_at
    assert a.params == b.params
    assert a.universe == b.universe
    assert len(a.correlations) == len(b.correlations)
    for x, y in zip(a.correlations, b.correlations, strict=True):
        assert correlation_matrices_equal(x, y)
        assert np.allclose(x.values, y.values)
    assert a.pairs == b.pairs
    assert a.sets_by_size == b.sets_by_size
    assert a.largest_set == b.largest_set
    assert a.largest_set_exact == b.largest_set_exact
    assert a.momentum == b.momentum
    assert a.quality == b.quality


def test_save_load_analysis_round_trip(store: RunStore):
    result = make_result()
    store.save_analysis(result)
    loaded = store.load_analysis(result.run_id)
    assert_results_equal(loaded, result)
    assert isinstance(loaded.params.windows, tuple)
    assert isinstance(loaded.params.momentum_weights, tuple)
    assert isinstance(loaded.quality.per_symbol, tuple)
    assert loaded.correlations[0].values.dtype == np.float64


def test_round_trip_without_largest_set_and_not_exact(store: RunStore):
    result = replace(make_result(), largest_set=None, largest_set_exact=False, sets_by_size={})
    store.save_analysis(result)
    assert_results_equal(store.load_analysis(result.run_id), result)


def test_round_trip_preserves_nan_floats(store: RunStore):
    base = make_result()
    nan_pair = replace(base.pairs[0], spearman_max_abs_rho=math.nan)
    result = replace(base, pairs=(nan_pair,))
    store.save_analysis(result)
    loaded = store.load_analysis(result.run_id)
    assert math.isnan(loaded.pairs[0].spearman_max_abs_rho)


def test_load_analysis_unknown_raises(store: RunStore):
    with pytest.raises(StoreError):
        store.load_analysis("missing")


def test_save_analysis_twice_raises_and_keeps_original(store: RunStore):
    result = make_result()
    store.save_analysis(result)
    with pytest.raises(StoreError):
        store.save_analysis(result)
    assert_results_equal(store.load_analysis(result.run_id), result)


def test_latest_run_id(store: RunStore):
    assert store.latest_run_id() is None
    store.save_analysis(make_result("old", NOW - timedelta(days=1)))
    store.save_analysis(make_result("new", NOW))
    store.save_analysis(make_result("older", NOW - timedelta(days=2)))
    assert store.latest_run_id() == "new"


def test_portfolio_round_trip(store: RunStore):
    store.save_analysis(make_result())
    ctx = PortfolioContext(
        account=AccountSnapshot(NOW, 1000.0, 5000.0, 2000.0, 5000.0, 4900.0, "USD", True),
        positions=(
            PositionSnapshot("AAA", 10.0, 100.0, 101.0, 1010.0, 1000.0, 10.0, 0.01, 0.005),
            PositionSnapshot("BBB", 5.0, 50.0, 49.0, 245.0, 250.0, -5.0, -0.02, -0.01),
        ),
        error=None,
    )
    store.save_portfolio("a" * 32, ctx)
    assert store.load_portfolio("a" * 32) == ctx


def test_portfolio_error_round_trip(store: RunStore):
    store.save_analysis(make_result())
    ctx = PortfolioContext(account=None, positions=(), error="401 unauthorized")
    store.save_portfolio("a" * 32, ctx)
    assert store.load_portfolio("a" * 32) == ctx


def test_load_portfolio_missing_raises(store: RunStore):
    with pytest.raises(StoreError):
        store.load_portfolio("nope")


def test_save_portfolio_unknown_run_raises(store: RunStore):
    ctx = PortfolioContext(account=None, positions=(), error="x")
    with pytest.raises(StoreError):
        store.save_portfolio("nope", ctx)


def test_job_started_idempotent(store: RunStore):
    assert store.job_started("send", date(2026, 10, 14)) is True
    assert store.job_started("send", date(2026, 10, 14)) is False
    assert store.job_started("send", date(2026, 10, 15)) is True
    assert store.job_started("refresh", date(2026, 10, 14)) is True


def test_job_finished_records_status(store: RunStore, clock: FakeClock):
    store.job_started("send", date(2026, 10, 14))
    clock.advance(30)
    store.job_finished("send", date(2026, 10, 14), "ok", "sent run abc")
    row = store.conn.execute(
        "SELECT status, started_at, finished_at, detail FROM job_runs WHERE job='send'"
    ).fetchone()
    assert row["status"] == "ok"
    assert datetime.fromisoformat(row["started_at"]) == NOW
    assert datetime.fromisoformat(row["finished_at"]) == NOW + timedelta(seconds=30)
    assert row["detail"] == "sent run abc"
    assert store.job_started("send", date(2026, 10, 14)) is False


def test_email_sent_for_flips_after_log(store: RunStore):
    store.save_analysis(make_result())
    assert store.email_sent_for("a" * 32) is False
    receipt = SendReceipt("resend", "msg-123", NOW)
    store.log_email("a" * 32, receipt, ["me@example.com", "you@example.com"])
    assert store.email_sent_for("a" * 32) is True
    assert store.email_sent_for("b" * 32) is False


def test_api_usage(store: RunStore):
    day = date(2026, 10, 14)
    assert store.api_usage_today("twelvedata", day) == 0
    store.add_api_usage("twelvedata", day, 3)
    store.add_api_usage("twelvedata", day, 4)
    assert store.api_usage_today("twelvedata", day) == 7
    assert store.api_usage_today("twelvedata", date(2026, 10, 15)) == 0
    assert store.api_usage_today("alpaca", day) == 0


def test_previous_run_id(store: RunStore):
    assert store.previous_run_id("missing") is None
    store.save_analysis(make_result("old", NOW - timedelta(days=1)))
    store.save_analysis(make_result("new", NOW))
    store.save_analysis(make_result("older", NOW - timedelta(days=2)))
    assert store.previous_run_id("new") == "old"
    assert store.previous_run_id("old") == "older"
    assert store.previous_run_id("older") is None
    assert store.previous_run_id("missing") is None


def test_previous_run_id_same_created_at_uses_insertion_order(store: RunStore):
    store.save_analysis(make_result("first", NOW))
    store.save_analysis(make_result("second", NOW))
    assert store.previous_run_id("second") == "first"
    assert store.previous_run_id("first") is None


def test_job_started_reclaims_retry_rows(store: RunStore, clock: FakeClock):
    store.job_started("send", date(2026, 10, 14))
    store.job_finished("send", date(2026, 10, 14), "retry", "EmailSendError('timeout')")
    clock.advance(300)
    assert store.job_started("send", date(2026, 10, 14)) is True
    row = store.conn.execute("SELECT status, started_at, finished_at FROM job_runs").fetchone()
    assert row["status"] == "running"
    assert datetime.fromisoformat(row["started_at"]) == NOW + timedelta(seconds=300)
    assert row["finished_at"] is None
    # Final statuses are never reclaimed.
    for status in ("ok", "error", "missed"):
        store.job_finished("send", date(2026, 10, 14), status, "")
        assert store.job_started("send", date(2026, 10, 14), timedelta(seconds=1)) is False


def test_job_started_reclaims_stale_running_row(store: RunStore, clock: FakeClock):
    # A crash mid-job leaves the row "running" for ever; it is reclaimed once stale.
    assert store.job_started("send", date(2026, 10, 14)) is True
    clock.advance(3600)
    assert store.job_started("send", date(2026, 10, 14), timedelta(hours=3)) is False
    assert store.job_started("send", date(2026, 10, 14)) is False
    clock.advance(2 * 3600 + 1)
    assert store.job_started("send", date(2026, 10, 14), timedelta(hours=3)) is True
    assert store.job_started("send", date(2026, 10, 14), timedelta(hours=3)) is False


def test_latest_run_id_prefers_latest_as_of_day(store: RunStore):
    # A historical --as-of run created later must not become "latest" (M1).
    current = make_result("current", NOW)
    store.save_analysis(current)
    old_as_of = replace(current.as_of, day=date(2026, 6, 1))
    store.save_analysis(replace(make_result("historic", NOW + timedelta(hours=1)), as_of=old_as_of))
    assert store.latest_run_id() == "current"
    store.save_analysis(make_result("rerun", NOW + timedelta(hours=2)))
    assert store.latest_run_id() == "rerun"


def test_email_count(store: RunStore):
    store.save_analysis(make_result())
    assert store.email_count("a" * 32) == 0
    receipt = SendReceipt("smtp", "m1", NOW)
    store.log_email("a" * 32, receipt, ["x@example.com"])
    store.log_email("a" * 32, receipt, ["x@example.com"])
    assert store.email_count("a" * 32) == 2
