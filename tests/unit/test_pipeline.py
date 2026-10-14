"""Unit tests for the run_analysis pipeline."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from conftest import make_bars
from uncorrelated_trading.analysis.pipeline import run_analysis
from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import AnalysisParams, AsOf, Bar, CorrelationMatrix, SymbolQuality

END = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)
SYMBOLS = [f"S{i:02d}" for i in range(10)]


def params(**overrides: object) -> AnalysisParams:
    base: dict[str, object] = {
        "primary_window": 60,
        "windows": (60, 120, 250),
        "tau": 0.20,
        "tau_spearman": 0.30,
        "max_set_size_exact": 4,
        "max_sets_per_size": 50,
        "search_time_budget_s": 5.0,
        "momentum_k": 20,
        "momentum_long": 120,
        "momentum_skip": 20,
        "momentum_weights": (0.4, 0.3, 0.3),
        "direction_threshold": 0.5,
    }
    base.update(overrides)
    return AnalysisParams(**base)  # type: ignore[arg-type]


def as_of(day: date = END) -> AsOf:
    return AsOf(
        day=day,
        mode="post_close",
        now_utc=NOW,
        provisional=False,
        calendar_source="static",
        note="test",
    )


def matrix(
    result_corr: tuple[CorrelationMatrix, ...], window: int, method: str
) -> CorrelationMatrix:
    found = [c for c in result_corr if c.window == window and c.method == method]
    assert len(found) == 1
    return found[0]


def test_pipeline_end_to_end_on_sample_bars() -> None:
    bars = make_bars(SYMBOLS, 300, seed=1)
    result = run_analysis(bars, SYMBOLS, as_of(), params(), NOW)
    keys = sorted((c.window, c.method) for c in result.correlations)
    assert keys == sorted((w, m) for w in (60, 120, 250) for m in ("pearson", "spearman"))
    assert len(result.pairs) > 0
    assert result.universe == tuple(SYMBOLS)
    assert result.quality.analysed == tuple(SYMBOLS)
    assert result.quality.requested == tuple(SYMBOLS)
    assert sorted(m.symbol for m in result.momentum) == sorted(SYMBOLS)
    assert [m.rank for m in result.momentum] == list(range(1, len(SYMBOLS) + 1))
    assert re.fullmatch(r"[0-9a-f]{32}", result.run_id)
    assert result.created_at == NOW
    assert set(result.sets_by_size) == {3, 4}
    primary = matrix(result.correlations, 60, "pearson")
    assert primary.end_day == END
    assert primary.n_obs == 60
    for pair in result.pairs:
        assert pair.max_abs_rho < 0.20


def test_run_id_passed_through() -> None:
    bars = make_bars(SYMBOLS[:5], 300, seed=2)
    result = run_analysis(bars, SYMBOLS[:5], as_of(), params(), NOW, run_id="abc")
    assert result.run_id == "abc"


def test_symbol_with_gap_is_dropped_and_reported() -> None:
    bars = make_bars(SYMBOLS[:6], 300, seed=3)
    gap_day = sorted({b.day for b in bars})[-10]
    bars = [b for b in bars if not (b.symbol == "S02" and b.day == gap_day)]
    result = run_analysis(bars, SYMBOLS[:6], as_of(), params(), NOW)
    assert "S02" not in result.quality.analysed
    for cm in result.correlations:
        assert "S02" not in cm.symbols
    assert "S02" not in {m.symbol for m in result.momentum}
    rows = [q for q in result.quality.per_symbol if q.symbol == "S02"]
    assert len(rows) == 1
    assert rows[0].status == "dropped_missing"


def test_too_few_symbols_raises() -> None:
    bars = make_bars(["AAA", "BBB"], 300, seed=4)
    with pytest.raises(AnalysisError):
        run_analysis(bars, ["AAA", "BBB"], as_of(), params(), NOW)


def test_too_few_symbols_after_drops_raises() -> None:
    syms = ["AAA", "BBB", "CCC", "DDD"]
    bars = make_bars(syms, 300, seed=5)
    gap_day = sorted({b.day for b in bars})[-5]
    bars = [b for b in bars if not (b.symbol in {"CCC", "DDD"} and b.day == gap_day)]
    with pytest.raises(AnalysisError):
        run_analysis(bars, syms, as_of(), params(), NOW)


def test_quality_in_is_merged() -> None:
    syms = SYMBOLS[:5]
    bars = make_bars(syms, 300, seed=6)
    gap_day = sorted({b.day for b in bars})[-3]
    bars = [b for b in bars if not (b.symbol == "S04" and b.day == gap_day)]
    quality_in = (
        SymbolQuality("S00", "ok", "", 300, "alpaca"),
        SymbolQuality("S01", "fallback_used", "alpaca failed", 300, "twelvedata"),
        SymbolQuality("S04", "ok", "", 300, "alpaca"),
        SymbolQuality("ZZZ", "dropped_missing", "no bars returned", 0, ""),
    )
    universe = [*syms, "ZZZ"]
    result = run_analysis(bars, universe, as_of(), params(), NOW, quality_in=quality_in)
    q = result.quality
    assert q.requested == tuple(universe)
    assert q.analysed == ("S00", "S01", "S02", "S03")
    by_symbol = {r.symbol: r for r in q.per_symbol}
    assert len(by_symbol) == len(q.per_symbol)  # one row per symbol
    assert by_symbol["S00"].status == "ok"
    assert by_symbol["S01"].status == "fallback_used"
    assert by_symbol["S04"].status == "dropped_missing"  # analysis drop wins over "ok"
    assert by_symbol["ZZZ"].status == "dropped_missing"
    assert by_symbol["ZZZ"].detail == "no bars returned"
    assert [r.symbol for r in q.per_symbol][:2] == ["S00", "S01"]
    assert q.fallback_calls == 1
    assert q.primary_source_failures == 1


def test_short_history_symbol_excluded_only_from_long_windows() -> None:
    old = SYMBOLS[:5]
    bars: list[Bar] = make_bars(old, 300, seed=7) + make_bars(["NEW"], 100, seed=8)
    universe = [*old, "NEW"]
    result = run_analysis(bars, universe, as_of(), params(), NOW)
    assert "NEW" in matrix(result.correlations, 60, "pearson").symbols
    assert "NEW" in matrix(result.correlations, 60, "spearman").symbols
    for w in (120, 250):
        for method in ("pearson", "spearman"):
            assert "NEW" not in matrix(result.correlations, w, method).symbols
    assert "NEW" in result.quality.analysed
    assert "NEW" not in {m.symbol for m in result.momentum}


def test_limited_history_still_produces_primary_window() -> None:
    syms = SYMBOLS[:5]
    bars = make_bars(syms, 90, seed=9)
    result = run_analysis(bars, syms, as_of(), params(), NOW)
    assert {c.window for c in result.correlations} == {60}
    assert result.momentum == ()
    assert result.quality.analysed == tuple(syms)
    for s in result.pairs:
        assert not s.stable_in_long_window


def test_insufficient_history_for_primary_raises() -> None:
    syms = SYMBOLS[:5]
    bars = make_bars(syms, 40, seed=10)
    with pytest.raises(AnalysisError):
        run_analysis(bars, syms, as_of(), params(), NOW)


def test_bars_after_as_of_are_ignored() -> None:
    syms = SYMBOLS[:5]
    bars = make_bars(syms, 300, seed=11)
    earlier = sorted({b.day for b in bars})[-6]
    result = run_analysis(bars, syms, as_of(earlier), params(), NOW)
    for cm in result.correlations:
        assert cm.end_day == earlier


def test_largest_set_fields_follow_search() -> None:
    bars = make_bars(SYMBOLS, 300, seed=12)
    result = run_analysis(bars, SYMBOLS, as_of(), params(), NOW)
    assert result.largest_set is not None
    assert result.largest_set_exact is True
    primary = matrix(result.correlations, 60, "pearson")
    idx = {s: i for i, s in enumerate(primary.symbols)}
    members = result.largest_set.symbols
    for i, a in enumerate(members):
        for b in members[i + 1 :]:
            assert abs(primary.values[idx[a], idx[b]]) < 0.20
    mz = {m.symbol: m.composite_z for m in result.momentum}
    expected = sum(mz[s] for s in members) / len(members)
    assert result.largest_set.mean_momentum_z == pytest.approx(expected)


def test_naive_now_rejected() -> None:
    bars = make_bars(SYMBOLS[:5], 300, seed=13)
    with pytest.raises(AnalysisError):
        run_analysis(bars, SYMBOLS[:5], as_of(), params(), datetime(2026, 10, 13))  # noqa: DTZ001


def _partial_case(
    quality_in: tuple[SymbolQuality, ...] = (),
) -> tuple[list[Bar], list[str], tuple[SymbolQuality, ...]]:
    old = SYMBOLS[:5]
    bars: list[Bar] = make_bars(old, 300, seed=14) + make_bars(["NEW"], 100, seed=15)
    return bars, [*old, "NEW"], quality_in


def test_partially_excluded_symbol_is_not_marked_dropped() -> None:
    bars, universe, _ = _partial_case()
    result = run_analysis(bars, universe, as_of(), params(), NOW)
    q = result.quality
    assert "NEW" in q.analysed
    rows = [r for r in q.per_symbol if r.symbol == "NEW"]
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].detail.startswith("analysed in the 60-day window; excluded from ")
    assert "120-day window" in rows[0].detail
    assert "250-day window" in rows[0].detail
    assert "momentum" in rows[0].detail
    assert rows[0].source == "csv"
    analysed = set(q.analysed)
    assert not any(r.status.startswith("dropped") for r in q.per_symbol if r.symbol in analysed)


def test_partially_excluded_symbol_keeps_fetch_status() -> None:
    fetch = (
        SymbolQuality("NEW", "fallback_used", "alpaca failed", 100, "twelvedata"),
        SymbolQuality("S00", "dropped_short_history", "stale fetch note", 300, "alpaca"),
    )
    bars, universe, quality_in = _partial_case(fetch)
    result = run_analysis(bars, universe, as_of(), params(), NOW, quality_in=quality_in)
    q = result.quality
    by_symbol = {r.symbol: r for r in q.per_symbol}
    assert len(by_symbol) == len(q.per_symbol)
    new = by_symbol["NEW"]
    assert new.status == "fallback_used"
    assert new.source == "twelvedata"
    assert new.detail.startswith("alpaca failed; analysed in the 60-day window; excluded from ")
    assert "momentum" in new.detail
    analysed = set(q.analysed)
    assert {"NEW", "S00"} <= analysed
    assert not any(r.status.startswith("dropped") for r in q.per_symbol if r.symbol in analysed)
    assert q.fallback_calls == 1


def test_as_of_day_without_bars_raises() -> None:
    # Bars end on 2026-10-13; an as-of of 2026-10-14 must not be analysed on older data
    # and labelled with the as-of day (even when fetch-time records say the bar is missing).
    bars = make_bars(SYMBOLS, 300, seed=16)
    missing = date(2026, 10, 14)
    quality_in = tuple(
        SymbolQuality(s, "dropped_missing", f"no bar for {missing}", 299, "") for s in SYMBOLS
    )
    with pytest.raises(
        AnalysisError, match="no bars for as-of day 2026-10-14; last available day is 2026-10-13"
    ):
        run_analysis(bars, SYMBOLS, as_of(missing), params(), NOW, quality_in=quality_in)


def test_stray_extra_day_bar_does_not_abort_run() -> None:
    # A single fallback bar on a day nobody else traded must not drop every other symbol.
    bars = make_bars(SYMBOLS, 300, seed=17)
    stray = Bar("S01", date(2026, 10, 10), 50.0, 51.0, 49.0, 50.0, 1, "twelvedata")  # Saturday
    result = run_analysis([*bars, stray], SYMBOLS, as_of(), params(), NOW)
    assert result.quality.analysed == tuple(SYMBOLS)
    assert matrix(result.correlations, 60, "pearson").end_day == END
