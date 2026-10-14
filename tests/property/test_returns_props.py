from datetime import date, timedelta

import numpy as np
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from conftest import make_bars
from uncorrelated_trading.analysis.returns import build_price_matrix, log_returns
from uncorrelated_trading.models import Bar

END = date(2026, 10, 13)

universes = st.tuples(
    st.integers(min_value=3, max_value=6),
    st.integers(min_value=30, max_value=80),
    st.integers(min_value=0, max_value=10_000),
)


@settings(max_examples=50, deadline=None)
@given(universes, st.data())
def test_window_shape_and_no_nan(params: tuple[int, int, int], data: st.DataObject) -> None:
    n_sym, n_bars, seed = params
    symbols = [f"S{i}" for i in range(n_sym)]
    bars = make_bars(symbols, n_bars, seed=seed)
    n_days = data.draw(st.integers(min_value=1, max_value=n_bars - 1))
    res = build_price_matrix(bars, symbols, END, n_days)
    assert res.dropped == ()
    assert res.prices.shape == (n_days + 1, n_sym)
    assert not res.prices.isna().any().any()
    rets = log_returns(res.prices)
    assert rets.shape == (n_days, n_sym)


@settings(max_examples=50, deadline=None)
@given(universes, st.data())
def test_removing_a_window_bar_drops_exactly_that_symbol(
    params: tuple[int, int, int], data: st.DataObject
) -> None:
    n_sym, n_bars, seed = params
    symbols = [f"S{i}" for i in range(n_sym)]
    bars = make_bars(symbols, n_bars, seed=seed)
    n_days = n_bars - 1
    victim = data.draw(st.sampled_from(symbols))
    days = sorted({b.day for b in bars})
    # Keep the first and last day so the window boundaries are unchanged.
    gap = data.draw(st.sampled_from(days[1:-1]))
    kept: list[Bar] = [b for b in bars if not (b.symbol == victim and b.day == gap)]
    res = build_price_matrix(kept, symbols, END, n_days)
    assert [q.symbol for q in res.dropped] == [victim]
    assert res.dropped[0].status == "dropped_missing"
    assert list(res.prices.columns) == [s for s in symbols if s != victim]


@settings(max_examples=50, deadline=None)
@given(universes, st.floats(min_value=1e-3, max_value=1e3))
def test_log_returns_scale_invariant(params: tuple[int, int, int], scale: float) -> None:
    n_sym, n_bars, seed = params
    symbols = [f"S{i}" for i in range(n_sym)]
    prices = build_price_matrix(make_bars(symbols, n_bars, seed=seed), symbols, END, n_bars - 1)
    scaled = prices.prices * scale
    np.testing.assert_allclose(
        log_returns(scaled).to_numpy(), log_returns(prices.prices).to_numpy(), atol=1e-9
    )


@settings(max_examples=50, deadline=None)
@given(universes, st.data())
def test_extra_day_bar_for_one_symbol_changes_nothing_else(
    params: tuple[int, int, int], data: st.DataObject
) -> None:
    n_sym, n_bars, seed = params
    symbols = [f"S{i}" for i in range(n_sym)]
    bars = make_bars(symbols, n_bars, seed=seed)
    n_days = data.draw(st.integers(min_value=1, max_value=n_bars - 1))
    days = sorted({b.day for b in bars})
    victim = data.draw(st.sampled_from(symbols))
    offset = data.draw(st.integers(min_value=-10, max_value=(END - days[0]).days))
    extra_day = days[0] + timedelta(days=offset)
    assume(extra_day not in set(days))
    stray = Bar(victim, extra_day, 50.0, 51.0, 49.0, 50.0, 1, "twelvedata")
    base = build_price_matrix(bars, symbols, END, n_days)
    res = build_price_matrix([*bars, stray], symbols, END, n_days)
    assert list(res.prices.index) == list(base.prices.index)
    others = [s for s in base.prices.columns if s != victim]
    assert [s for s in res.prices.columns if s != victim] == others
    np.testing.assert_array_equal(res.prices[others].to_numpy(), base.prices[others].to_numpy())
    assert [q for q in res.dropped if q.symbol != victim] == [
        q for q in base.dropped if q.symbol != victim
    ]
