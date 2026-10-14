"""Property-based tests for composite momentum scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.analysis.momentum import momentum_scores
from uncorrelated_trading.models import AnalysisParams, MomentumScore


def make_params(weights: tuple[float, float, float] = (0.4, 0.3, 0.3)) -> AnalysisParams:
    return AnalysisParams(
        primary_window=60,
        windows=(60, 120, 250),
        tau=0.20,
        tau_spearman=0.30,
        max_set_size_exact=4,
        max_sets_per_size=50,
        search_time_budget_s=20.0,
        momentum_k=20,
        momentum_long=120,
        momentum_skip=20,
        momentum_weights=weights,
        direction_threshold=0.5,
    )


@st.composite
def return_frames(draw: st.DrawFn) -> pd.DataFrame:
    n_sym = draw(st.integers(min_value=3, max_value=8))
    n_rows = draw(st.integers(min_value=120, max_value=200))
    seed = draw(st.integers(min_value=0, max_value=2**32 - 1))
    rng = np.random.default_rng(seed)
    mus = rng.normal(0.0, 0.001, size=n_sym)
    sds = rng.uniform(0.005, 0.03, size=n_sym)
    data = rng.normal(mus, sds, size=(n_rows, n_sym))
    cols = [f"S{i}" for i in range(n_sym)]
    idx = pd.bdate_range("2026-01-02", periods=n_rows)
    return pd.DataFrame(data, index=idx, columns=cols)


weights_summing_to_one = st.tuples(
    st.floats(min_value=0.05, max_value=1.0),
    st.floats(min_value=0.05, max_value=1.0),
    st.floats(min_value=0.05, max_value=1.0),
).map(lambda w: (w[0] / sum(w), w[1] / sum(w), w[2] / sum(w)))


def by_symbol(scores: tuple[MomentumScore, ...]) -> dict[str, MomentumScore]:
    return {s.symbol: s for s in scores}


@settings(max_examples=60, deadline=None)
@given(df=return_frames(), weights=weights_summing_to_one)
def test_composite_mean_is_zero(df: pd.DataFrame, weights: tuple[float, float, float]) -> None:
    # With n <= 8 symbols a population z-score is bounded by sqrt(n - 1) < 3,
    # so winsorisation never binds and the composite mean is exactly 0.
    scores = momentum_scores(df, make_params(weights))
    assert abs(float(np.mean([s.composite_z for s in scores]))) < 1e-9
    assert sorted(s.rank for s in scores) == list(range(1, len(scores) + 1))


@settings(max_examples=60, deadline=None)
@given(df=return_frames(), shift=st.floats(min_value=-0.01, max_value=0.01))
def test_constant_return_shift_leaves_ranks_unchanged(df: pd.DataFrame, shift: float) -> None:
    # Shifting every return by c shifts trailing and skip returns uniformly, so
    # their z-scores are unchanged; vol-adjusted return is not shift-invariant,
    # so its weight is zero here.
    params = make_params((0.5, 0.5, 0.0))
    base = by_symbol(momentum_scores(df, params))
    moved = by_symbol(momentum_scores(df + shift, params))
    for sym, s in base.items():
        assert moved[sym].rank == s.rank
        assert abs(moved[sym].composite_z - s.composite_z) < 1e-8


@settings(max_examples=60, deadline=None)
@given(df=return_frames(), offset=st.floats(min_value=-5.0, max_value=5.0))
def test_log_price_offset_leaves_scores_unchanged(df: pd.DataFrame, offset: float) -> None:
    # Adding a constant to all log prices leaves log returns, hence scores, unchanged.
    start = pd.DataFrame(0.0, index=[df.index[0] - pd.Timedelta(days=1)], columns=df.columns)
    log_prices = pd.concat([start, df.cumsum()]) + offset
    rebuilt = log_prices.diff().iloc[1:]
    base = by_symbol(momentum_scores(df, make_params()))
    moved = by_symbol(momentum_scores(rebuilt, make_params()))
    for sym, s in base.items():
        assert moved[sym].rank == s.rank
        assert abs(moved[sym].composite_z - s.composite_z) < 1e-8


@settings(max_examples=60, deadline=None)
@given(df=return_frames(), data=st.data())
def test_permutation_invariance(df: pd.DataFrame, data: st.DataObject) -> None:
    perm = data.draw(st.permutations(list(df.columns)))
    base = by_symbol(momentum_scores(df, make_params()))
    permuted = by_symbol(momentum_scores(df[perm], make_params()))
    assert base.keys() == permuted.keys()
    for sym, s in base.items():
        p = permuted[sym]
        assert p.rank == s.rank
        assert p.direction == s.direction
        assert p.trailing_return == s.trailing_return
        assert p.skip_return == s.skip_return
        assert abs(p.composite_z - s.composite_z) < 1e-12
