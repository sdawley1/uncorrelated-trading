"""End-to-end analysis pipeline: bars in, :class:`AnalysisResult` out.

``run_analysis`` is the only analysis entry point orchestration calls. It aligns close
prices per correlation window (so a symbol with a short history is excluded only from the
windows it cannot support), computes Pearson and Spearman matrices for every configured
window, scores momentum over ``momentum_long`` returns, searches for uncorrelated pairs and
sets on the primary window, and merges fetch-time data-quality records with the symbols the
analysis had to drop. The output is advisory only.
"""

from __future__ import annotations

# ============================================================
# IMPORTS
# ============================================================
import logging
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime

from uncorrelated_trading.analysis.correlation import correlation_matrix
from uncorrelated_trading.analysis.momentum import momentum_scores
from uncorrelated_trading.analysis.returns import build_price_matrix, log_returns, window_days
from uncorrelated_trading.analysis.uncorrelated import search_uncorrelated_sets
from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import (
    AnalysisParams,
    AnalysisResult,
    AsOf,
    Bar,
    CorrelationMatrix,
    DataQualityReport,
    MomentumScore,
    SymbolQuality,
)

__all__ = ["MIN_ANALYSED_SYMBOLS", "run_analysis"]

# ============================================================
# CONSTANTS
# ============================================================

logger = logging.getLogger(__name__)

MIN_ANALYSED_SYMBOLS = 3
_MIN_WINDOW_SYMBOLS = 2
_OVERRIDABLE_STATUSES = frozenset({"ok", "fallback_used"})

# ============================================================
# PUBLIC API
# ============================================================


def run_analysis(
    bars: Sequence[Bar],
    universe: Sequence[str],
    as_of: AsOf,
    params: AnalysisParams,
    now_utc: datetime,
    quality_in: Sequence[SymbolQuality] = (),
    run_id: str | None = None,
    primary_source_failures: int | None = None,
    fallback_calls: int | None = None,
) -> AnalysisResult:
    """Run the full correlation, uncorrelated-set and momentum analysis.

    Parameters
    ----------
    bars : Sequence[Bar]
        Daily bars for (a superset of) ``universe``; bars after ``as_of.day`` are ignored.
    universe : Sequence[str]
        Requested symbols in display order.
    as_of : AsOf
        Resolved as-of day; every window ends on ``as_of.day``.
    params : AnalysisParams
        Windows, thresholds, search limits and momentum parameters.
    now_utc : datetime
        Tz-aware creation instant stored as ``created_at``.
    quality_in : Sequence[SymbolQuality], optional
        Data-quality records from the fetch step, merged into the report.
    run_id : str or None, optional
        Run identifier; defaults to ``uuid.uuid4().hex``.
    primary_source_failures : int or None, optional
        The composite source's primary-failure count for the run; when None, the number of
        ``fallback_used`` records in ``quality_in`` is used.
    fallback_calls : int or None, optional
        The composite source's fallback request count; when None, counted as above.

    Returns
    -------
    AnalysisResult
        Pearson and Spearman matrices for every window that at least two symbols support
        (ascending window, Pearson first), uncorrelated pairs and sets from the primary
        window, momentum scores for symbols with ``momentum_long`` returns, and a merged
        :class:`DataQualityReport`.

    Raises
    ------
    AnalysisError
        If ``now_utc`` is naive, fewer than ``primary_window + 1`` trading days exist up to
        ``as_of.day``, the price window does not end on ``as_of.day`` (no bars for the as-of
        day: the run is never silently analysed on older data), or fewer than three symbols
        survive the primary window.
    """
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise AnalysisError("now_utc must be timezone-aware")
    end_day = as_of.day
    primary = params.primary_window
    available = _available_returns(bars, universe, end_day)
    if available < primary:
        raise AnalysisError(
            f"primary window needs {primary + 1} trading days up to {end_day.isoformat()}, "
            f"found {available + 1}"
        )

    # ----- correlation matrices per window -----
    correlations: list[CorrelationMatrix] = []
    pearson_by_window: dict[int, CorrelationMatrix] = {}
    spearman_by_window: dict[int, CorrelationMatrix] = {}
    window_drops: dict[int, dict[str, SymbolQuality]] = {}
    for window in sorted({*params.windows, primary}):
        if window > available:
            logger.warning(
                "skipping %d-day window: only %d trading days of returns up to %s",
                window,
                available,
                end_day.isoformat(),
            )
            continue
        built = build_price_matrix(bars, universe, end_day, window)
        last_day = built.prices.index[-1]
        if last_day != end_day:
            raise AnalysisError(
                f"no bars for as-of day {end_day.isoformat()}; "
                f"last available day is {last_day.isoformat()}"
            )
        window_drops[window] = {q.symbol: q for q in built.dropped}
        if built.prices.shape[1] < _MIN_WINDOW_SYMBOLS:
            if window == primary:
                break
            logger.warning(
                "skipping %d-day window: %d symbols have full history",
                window,
                built.prices.shape[1],
            )
            continue
        returns = log_returns(built.prices)
        pearson = correlation_matrix(returns, "pearson", window)
        spearman = correlation_matrix(returns, "spearman", window)
        pearson_by_window[window] = pearson
        spearman_by_window[window] = spearman
        correlations.extend((pearson, spearman))

    primary_pearson = pearson_by_window.get(primary)
    if primary_pearson is None or len(primary_pearson.symbols) < MIN_ANALYSED_SYMBOLS:
        n_ok = 0 if primary_pearson is None else len(primary_pearson.symbols)
        raise AnalysisError(
            f"only {n_ok} symbols survive the {primary}-day primary window; "
            f"need at least {MIN_ANALYSED_SYMBOLS}"
        )
    analysed = primary_pearson.symbols
    long_window = max(pearson_by_window)
    long_pearson = pearson_by_window[long_window] if long_window != primary else None

    # ----- momentum -----
    momentum, momentum_drops = _momentum(bars, analysed, end_day, params, available)
    momentum_z = {m.symbol: m.composite_z for m in momentum}

    # ----- uncorrelated sets on the primary window -----
    search = search_uncorrelated_sets(
        primary_pearson,
        spearman_by_window[primary],
        long_pearson,
        params.tau,
        params.tau_spearman,
        params.max_set_size_exact,
        params.max_sets_per_size,
        params.search_time_budget_s,
        momentum_z=momentum_z,
    )
    if search.deadline_hit:
        logger.warning("uncorrelated-set search hit its %.1fs budget", params.search_time_budget_s)

    quality = _quality_report(universe, analysed, quality_in, primary, window_drops, momentum_drops)
    fallbacks = sum(1 for q in quality_in if q.status == "fallback_used")
    quality = replace(
        quality,
        primary_source_failures=(
            fallbacks if primary_source_failures is None else primary_source_failures
        ),
        fallback_calls=fallbacks if fallback_calls is None else fallback_calls,
    )
    return AnalysisResult(
        run_id=run_id if run_id is not None else uuid.uuid4().hex,
        as_of=as_of,
        created_at=now_utc,
        params=params,
        universe=tuple(universe),
        correlations=tuple(correlations),
        pairs=search.pairs,
        sets_by_size=search.sets_by_size,
        largest_set=search.largest,
        largest_set_exact=search.largest_exact,
        momentum=momentum,
        quality=quality,
    )


# ============================================================
# PRIVATE HELPERS
# ============================================================


def _available_returns(bars: Sequence[Bar], universe: Sequence[str], end_day: date) -> int:
    """Count the daily returns supported by the window days up to ``end_day``."""
    return max(len(window_days(bars, universe, end_day)) - 1, 0)


def _momentum(
    bars: Sequence[Bar],
    analysed: Sequence[str],
    end_day: date,
    params: AnalysisParams,
    available: int,
) -> tuple[tuple[MomentumScore, ...], dict[str, SymbolQuality]]:
    """Score momentum for analysed symbols with ``momentum_long`` returns of history.

    Returns the scores (empty when history or symbols are insufficient) and the drop
    records of analysed symbols that lack the momentum history.
    """
    long_ = params.momentum_long
    if long_ > available:
        logger.warning(
            "skipping momentum: needs %d returns, only %d available up to %s",
            long_,
            available,
            end_day.isoformat(),
        )
        return (), {}
    built = build_price_matrix(bars, analysed, end_day, long_)
    drops = {q.symbol: q for q in built.dropped}
    if built.prices.shape[1] < _MIN_WINDOW_SYMBOLS:
        logger.warning("skipping momentum: %d symbols have full history", built.prices.shape[1])
        return (), drops
    return momentum_scores(log_returns(built.prices), params), drops


def _quality_report(
    universe: Sequence[str],
    analysed: tuple[str, ...],
    quality_in: Sequence[SymbolQuality],
    primary: int,
    window_drops: dict[int, dict[str, SymbolQuality]],
    momentum_drops: dict[str, SymbolQuality],
) -> DataQualityReport:
    """Merge fetch-time quality records with the analysis drops (one row per symbol).

    A symbol dropped from the primary window carries that window's ``dropped_*`` record,
    which replaces an ``ok``/``fallback_used`` fetch record (any other fetch record is kept
    instead). A symbol in ``analysed`` never carries a ``dropped_*`` status: if it was
    excluded from a longer window or from momentum, its fetch record (when present) keeps
    its status and gains the exclusion text in ``detail``; otherwise it gets an ``ok`` row
    whose detail names what it was excluded from.
    """
    analysed_set = set(analysed)
    primary_drops = {
        sym: q for sym, q in window_drops.get(primary, {}).items() if sym not in analysed_set
    }
    exclusions = _partial_exclusions(analysed, primary, window_drops, momentum_drops)

    rows: list[SymbolQuality] = []
    seen: set[str] = set()
    for q in quality_in:
        sym = q.symbol
        if sym in seen:
            continue
        if sym in primary_drops and q.status in _OVERRIDABLE_STATUSES:
            continue
        seen.add(sym)
        if sym in analysed_set and q.status.startswith("dropped"):
            rows.append(exclusions.get(sym) or replace(q, status="ok"))
        elif sym in exclusions:
            note = exclusions[sym].detail
            rows.append(replace(q, detail=f"{q.detail}; {note}" if q.detail else note))
        else:
            rows.append(q)
    analysis = {**primary_drops, **exclusions}
    order = {s: i for i, s in enumerate(universe)}
    for sym in sorted(analysis, key=lambda s: (order.get(s, len(order)), s)):
        if sym not in seen:
            seen.add(sym)
            rows.append(analysis[sym])
    fallbacks = sum(1 for q in quality_in if q.status == "fallback_used")
    return DataQualityReport(
        requested=tuple(universe),
        analysed=analysed,
        per_symbol=tuple(rows),
        primary_source_failures=fallbacks,
        fallback_calls=fallbacks,
    )


def _partial_exclusions(
    analysed: Sequence[str],
    primary: int,
    window_drops: dict[int, dict[str, SymbolQuality]],
    momentum_drops: dict[str, SymbolQuality],
) -> dict[str, SymbolQuality]:
    """Build ``ok`` rows for analysed symbols excluded from longer windows or momentum.

    The detail names every excluded window and momentum, followed by the drop detail of
    the longest excluding window (or of momentum); ``bars_available`` and ``source`` come
    from that same record.
    """
    out: dict[str, SymbolQuality] = {}
    for sym in analysed:
        excluded = [w for w, drops in sorted(window_drops.items()) if sym in drops]
        reason = window_drops[excluded[-1]][sym] if excluded else momentum_drops.get(sym)
        if reason is None:
            continue
        parts = [f"{w}-day window" for w in excluded]
        if sym in momentum_drops:
            parts.append("momentum")
        out[sym] = SymbolQuality(
            symbol=sym,
            status="ok",
            detail=(
                f"analysed in the {primary}-day window; excluded from "
                f"{', '.join(parts)}: {reason.detail}"
            ),
            bars_available=reason.bars_available,
            source=reason.source,
        )
    return out
