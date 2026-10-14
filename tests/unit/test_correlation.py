from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from uncorrelated_trading.analysis.correlation import correlation_matrix
from uncorrelated_trading.errors import AnalysisError


def _frame(data: dict[str, np.ndarray]) -> pd.DataFrame:
    n = len(next(iter(data.values())))
    idx = pd.Index([date(2026, 1, 1) + timedelta(days=i) for i in range(n)], name="date")
    return pd.DataFrame(data, index=idx)


def _random(n_rows: int = 80, n_cols: int = 4, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cols = {f"S{i}": rng.normal(0, 0.01, n_rows) for i in range(n_cols)}
    return _frame(cols)


@pytest.mark.parametrize("method", ["pearson", "spearman"])
def test_correlation_unit_diagonal_symmetric(method: str) -> None:
    cm = correlation_matrix(_random(), method, 60)  # type: ignore[arg-type]
    v = cm.values
    assert v.shape == (4, 4)
    assert (np.diag(v) == 1.0).all()
    assert (v == v.T).all()
    assert (np.abs(v) <= 1.0).all()
    assert cm.method == method
    assert cm.symbols == ("S0", "S1", "S2", "S3")


def test_window_uses_last_rows() -> None:
    r = _random(80)
    cm = correlation_matrix(r, "pearson", 60)
    assert cm.window == 60
    assert cm.n_obs == 60
    assert cm.start_day == r.index[20]
    assert cm.end_day == r.index[-1]
    expected = r.iloc[-60:].corr().to_numpy()
    np.testing.assert_allclose(cm.values, expected, atol=1e-12)


def test_identical_series_rho_one() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(0, 0.01, 60)
    cm = correlation_matrix(
        _frame({"A": x, "B": x.copy(), "C": rng.normal(0, 0.01, 60)}), "pearson", 60
    )
    assert cm.values[0, 1] == pytest.approx(1.0, abs=1e-12)
    assert cm.values[0, 1] <= 1.0


def test_anti_series_rho_minus_one() -> None:
    rng = np.random.default_rng(4)
    x = rng.normal(0, 0.01, 60)
    for method in ("pearson", "spearman"):
        cm = correlation_matrix(_frame({"A": x, "B": -x}), method, 60)  # type: ignore[arg-type]
        assert cm.values[0, 1] == pytest.approx(-1.0, abs=1e-12)
        assert cm.values[0, 1] >= -1.0


def test_too_few_rows_raises() -> None:
    with pytest.raises(AnalysisError):
        correlation_matrix(_random(30), "pearson", 60)


def test_too_few_columns_raises() -> None:
    with pytest.raises(AnalysisError):
        correlation_matrix(_random(80, 1), "pearson", 60)


def test_constant_series_correlation_is_zero() -> None:
    rng = np.random.default_rng(5)
    cm = correlation_matrix(
        _frame({"A": rng.normal(0, 0.01, 60), "Z": np.zeros(60)}), "pearson", 60
    )
    assert cm.values[0, 1] == 0.0
    assert cm.values[1, 1] == 1.0
    assert not np.isnan(cm.values).any()


def test_window_below_two_raises() -> None:
    with pytest.raises(AnalysisError):
        correlation_matrix(_random(), "pearson", 1)


def test_timestamp_index_yields_dates() -> None:
    r = _random(60)
    r.index = pd.DatetimeIndex([pd.Timestamp(d) for d in r.index], name="date")
    cm = correlation_matrix(r, "pearson", 60)
    assert cm.start_day == date(2026, 1, 1)
    assert type(cm.end_day) is date


def test_non_date_index_raises() -> None:
    r = _random(60).reset_index(drop=True)
    with pytest.raises(AnalysisError):
        correlation_matrix(r, "pearson", 60)
