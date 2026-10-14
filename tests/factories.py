"""Hand-built sample objects shared by report tests (deterministic, no I/O)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np

from uncorrelated_trading.config import ReportConfig
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
    SymbolQuality,
    UncorrelatedSet,
)
from uncorrelated_trading.report.inputs import BriefInputs

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE")
AS_OF_DAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


def sample_params() -> AnalysisParams:
    return AnalysisParams(
        primary_window=60,
        windows=(60, 120),
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
    )


def _corr(window: int, method: str) -> CorrelationMatrix:
    base = np.array(
        [
            [1.00, 0.10, 0.65, -0.05, 0.30],
            [0.10, 1.00, 0.15, 0.08, 0.55],
            [0.65, 0.15, 1.00, 0.12, 0.40],
            [-0.05, 0.08, 0.12, 1.00, -0.10],
            [0.30, 0.55, 0.40, -0.10, 1.00],
        ]
    )
    if method == "spearman":
        base = np.clip(base * 0.9, -1, 1)
        np.fill_diagonal(base, 1.0)
    return CorrelationMatrix(
        window=window,
        method=method,  # type: ignore[arg-type]
        symbols=SYMBOLS,
        values=base,
        n_obs=window,
        start_day=date(2026, 7, 20),
        end_day=AS_OF_DAY,
    )


def _set(symbols: tuple[str, ...], max_rho: float, mean_mom: float) -> UncorrelatedSet:
    return UncorrelatedSet(
        symbols=symbols,
        size=len(symbols),
        max_abs_rho=max_rho,
        mean_abs_rho=max_rho * 0.7,
        max_abs_rho_ci_upper=min(1.0, max_rho + 0.25),
        min_eigenvalue=1.0 - max_rho * (len(symbols) - 1),
        spearman_max_abs_rho=max_rho * 0.9,
        passes_spearman=True,
        stable_in_long_window=max_rho < 0.1,
        mean_momentum_z=mean_mom,
    )


def sample_analysis_result() -> AnalysisResult:
    momentum = (
        MomentumScore("DDD", 0.08, 0.15, 1.9, 0.22, 1.21, 1, "up"),
        MomentumScore("AAA", 0.03, 0.05, 0.8, 0.18, 0.45, 2, "flat"),
        MomentumScore("CCC", 0.01, 0.02, 0.2, 0.25, 0.05, 3, "flat"),
        MomentumScore("EEE", -0.02, 0.01, -0.5, 0.30, -0.40, 4, "flat"),
        MomentumScore("BBB", -0.06, -0.10, -1.4, 0.21, -1.31, 5, "down"),
    )
    quality = DataQualityReport(
        requested=(*SYMBOLS, "ZZZ"),
        analysed=SYMBOLS,
        per_symbol=(
            SymbolQuality("ZZZ", "dropped_missing", "missing 3 of 61 window days", 58, "alpaca"),
            SymbolQuality(
                "EEE", "fallback_used", "alpaca returned no bars; twelvedata used", 61, "twelvedata"
            ),
        ),
        primary_source_failures=1,
        fallback_calls=1,
    )
    return AnalysisResult(
        run_id="0123456789abcdef0123456789abcdef",
        as_of=AsOf(
            AS_OF_DAY,
            "post_close",
            NOW,
            False,
            "alpaca",
            "after today's close; using today's session",
        ),
        created_at=NOW,
        params=sample_params(),
        universe=(*SYMBOLS, "ZZZ"),
        correlations=(
            _corr(60, "pearson"),
            _corr(60, "spearman"),
            _corr(120, "pearson"),
            _corr(120, "spearman"),
        ),
        pairs=(
            _set(("AAA", "DDD"), 0.05, 0.83),
            _set(("BBB", "DDD"), 0.08, -0.05),
            _set(("AAA", "BBB"), 0.10, -0.43),
        ),
        sets_by_size={3: (_set(("AAA", "BBB", "DDD"), 0.10, 0.12),), 4: ()},
        largest_set=_set(("AAA", "BBB", "DDD"), 0.10, 0.12),
        largest_set_exact=True,
        momentum=momentum,
        quality=quality,
    )


def sample_portfolio() -> PortfolioContext:
    return PortfolioContext(
        account=AccountSnapshot(
            NOW, 25_000.0, 100_000.0, 50_000.0, 100_000.0, 99_000.0, "USD", True
        ),
        positions=(
            PositionSnapshot("AAA", 10.0, 95.0, 100.0, 1_000.0, 950.0, 50.0, 0.0526, 0.01),
            PositionSnapshot("XYZ", 5.0, 20.0, 18.0, 90.0, 100.0, -10.0, -0.10, -0.02),
        ),
        error=None,
    )


def sample_report_config() -> ReportConfig:
    return ReportConfig(
        top_pairs=15, top_sets_per_size=5, top_momentum=10, subject_prefix="[uncorrelated-trading]"
    )


def sample_brief_inputs(
    previous_largest: tuple[str, ...] | None = ("AAA", "CCC", "DDD"),
) -> BriefInputs:
    return BriefInputs(
        result=sample_analysis_result(),
        portfolio=sample_portfolio(),
        previous_largest=previous_largest,
        report_cfg=sample_report_config(),
        app_version="0.1.0",
    )
