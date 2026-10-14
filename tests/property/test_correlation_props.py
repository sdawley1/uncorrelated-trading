from datetime import date

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import make_bars
from uncorrelated_trading.analysis.correlation import correlation_matrix
from uncorrelated_trading.analysis.returns import build_price_matrix, log_returns

END = date(2026, 10, 13)
METHODS = st.sampled_from(["pearson", "spearman"])

universes = st.tuples(
    st.integers(min_value=3, max_value=6),
    st.integers(min_value=30, max_value=80),
    st.integers(min_value=0, max_value=10_000),
)


def _prices(n_sym: int, n_bars: int, seed: int):  # noqa: ANN202
    symbols = [f"S{i}" for i in range(n_sym)]
    return build_price_matrix(
        make_bars(symbols, n_bars, seed=seed), symbols, END, n_bars - 1
    ).prices


@settings(max_examples=50, deadline=None)
@given(universes, METHODS, st.data())
def test_entries_bounded_symmetric_unit_diag(
    params: tuple[int, int, int], method: str, data: st.DataObject
) -> None:
    n_sym, n_bars, seed = params
    rets = log_returns(_prices(n_sym, n_bars, seed))
    window = data.draw(st.integers(min_value=3, max_value=len(rets)))
    cm = correlation_matrix(rets, method, window)  # type: ignore[arg-type]
    v = cm.values
    assert v.shape == (n_sym, n_sym)
    assert (np.abs(v) <= 1.0).all()
    assert (v == v.T).all()
    assert (np.diag(v) == 1.0).all()
    assert cm.n_obs == window


@settings(max_examples=50, deadline=None)
@given(universes, METHODS, st.data())
def test_scaling_one_symbol_leaves_matrix_unchanged(
    params: tuple[int, int, int], method: str, data: st.DataObject
) -> None:
    n_sym, n_bars, seed = params
    prices = _prices(n_sym, n_bars, seed)
    col = data.draw(st.sampled_from(list(prices.columns)))
    scale = data.draw(st.floats(min_value=1e-3, max_value=1e3))
    scaled = prices.copy()
    scaled[col] = scaled[col] * scale
    window = n_bars - 1
    a = correlation_matrix(log_returns(prices), method, window)  # type: ignore[arg-type]
    b = correlation_matrix(log_returns(scaled), method, window)  # type: ignore[arg-type]
    np.testing.assert_allclose(a.values, b.values, atol=1e-9, rtol=0)


@settings(max_examples=50, deadline=None)
@given(universes, METHODS, st.data())
def test_permuting_symbols_permutes_matrix(
    params: tuple[int, int, int], method: str, data: st.DataObject
) -> None:
    n_sym, n_bars, seed = params
    rets = log_returns(_prices(n_sym, n_bars, seed))
    perm = data.draw(st.permutations(list(range(n_sym))))
    permuted = rets.iloc[:, perm]
    window = len(rets)
    a = correlation_matrix(rets, method, window)  # type: ignore[arg-type]
    b = correlation_matrix(permuted, method, window)  # type: ignore[arg-type]
    assert b.symbols == tuple(a.symbols[i] for i in perm)
    np.testing.assert_allclose(b.values, a.values[np.ix_(perm, perm)], atol=1e-12, rtol=0)
