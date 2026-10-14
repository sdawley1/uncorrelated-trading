"""Property tests for the run_analysis pipeline."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import make_bars
from uncorrelated_trading.analysis.pipeline import run_analysis
from uncorrelated_trading.models import AnalysisParams, AsOf, UncorrelatedSet

END = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


def _params(tau: float) -> AnalysisParams:
    return AnalysisParams(
        primary_window=60,
        windows=(60, 120, 250),
        tau=tau,
        tau_spearman=0.30,
        max_set_size_exact=4,
        max_sets_per_size=50,
        search_time_budget_s=5.0,
        momentum_k=20,
        momentum_long=120,
        momentum_skip=20,
        momentum_weights=(0.4, 0.3, 0.3),
        direction_threshold=0.5,
    )


AS_OF = AsOf(
    day=END,
    mode="post_close",
    now_utc=NOW,
    provisional=False,
    calendar_source="static",
    note="property test",
)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    n=st.integers(min_value=4, max_value=12),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    tau=st.floats(min_value=0.05, max_value=0.5),
)
def test_every_reported_set_satisfies_pairwise_bound(n: int, seed: int, tau: float) -> None:
    symbols = [f"P{i:02d}" for i in range(n)]
    bars = make_bars(symbols, 260, seed=seed)
    result = run_analysis(bars, symbols, AS_OF, _params(tau), NOW)
    primary = [c for c in result.correlations if c.window == 60 and c.method == "pearson"]
    assert len(primary) == 1
    pearson = primary[0]
    pos = {s: i for i, s in enumerate(pearson.symbols)}

    reported: list[UncorrelatedSet] = list(result.pairs)
    for size, sets in result.sets_by_size.items():
        assert 3 <= size <= 4
        for s in sets:
            assert s.size == size == len(s.symbols)
        reported.extend(sets)
    if result.largest_set is not None:
        reported.append(result.largest_set)

    for s in reported:
        assert list(s.symbols) == sorted(set(s.symbols))
        for a, b in itertools.combinations(s.symbols, 2):
            assert abs(float(pearson.values[pos[a], pos[b]])) < tau
    assert set(result.quality.analysed) == set(pearson.symbols)
    assert [m.rank for m in result.momentum] == list(range(1, len(result.momentum) + 1))
