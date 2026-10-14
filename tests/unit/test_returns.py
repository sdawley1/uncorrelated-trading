from datetime import date

import numpy as np
import pandas as pd
import pytest

from conftest import make_bars
from uncorrelated_trading.analysis.returns import (
    PriceMatrixResult,
    build_price_matrix,
    log_returns,
)
from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import Bar

END = date(2026, 10, 13)


def _days(bars: list[Bar], symbol: str) -> list[date]:
    return sorted(b.day for b in bars if b.symbol == symbol)


def test_price_matrix_shape_and_order(sample_bars: list[Bar]) -> None:
    res = build_price_matrix(sample_bars, ["CCC", "AAA", "BBB"], END, 60)
    assert isinstance(res, PriceMatrixResult)
    assert res.dropped == ()
    assert res.prices.shape == (61, 3)
    assert list(res.prices.columns) == ["CCC", "AAA", "BBB"]
    assert res.prices.index.name == "date"
    assert res.prices.index.is_monotonic_increasing
    assert res.prices.index[-1] == END
    assert all(dt == np.float64 for dt in res.prices.dtypes)
    expected = [b.close for b in sample_bars if b.symbol == "AAA"][-61:]
    np.testing.assert_allclose(res.prices["AAA"].to_numpy(), expected)


def test_end_day_excludes_later_bars(sample_bars: list[Bar]) -> None:
    days = _days(sample_bars, "AAA")
    end = days[-10]
    res = build_price_matrix(sample_bars, ["AAA", "BBB", "CCC"], end, 20)
    assert res.prices.index[-1] == end
    assert len(res.prices) == 21


def test_symbol_with_gap_is_dropped(sample_bars: list[Bar]) -> None:
    days = _days(sample_bars, "BBB")
    gap_day = days[-30]
    bars = [b for b in sample_bars if not (b.symbol == "BBB" and b.day == gap_day)]
    res = build_price_matrix(bars, ["AAA", "BBB", "CCC"], END, 60)
    assert list(res.prices.columns) == ["AAA", "CCC"]
    assert len(res.dropped) == 1
    q = res.dropped[0]
    assert q.symbol == "BBB"
    assert q.status == "dropped_missing"
    assert gap_day.isoformat() in q.detail
    assert "1" in q.detail
    assert q.bars_available == 60
    assert q.source == "csv"
    assert len(res.prices) == 61


def test_delisted_symbol_bars_stop_early_is_dropped_missing(sample_bars: list[Bar]) -> None:
    days = _days(sample_bars, "CCC")
    cutoff = days[-5]
    bars = [b for b in sample_bars if not (b.symbol == "CCC" and b.day > cutoff)]
    res = build_price_matrix(bars, ["AAA", "BBB", "CCC"], END, 60)
    assert list(res.prices.columns) == ["AAA", "BBB"]
    (q,) = res.dropped
    assert q.status == "dropped_missing"
    assert days[-4].isoformat() in q.detail
    assert "4" in q.detail
    assert q.bars_available == 57
    assert not res.prices.isna().any().any()


def test_short_history_symbol(sample_bars: list[Bar]) -> None:
    new = make_bars(["NEW"], 30, seed=5)
    res = build_price_matrix(sample_bars + new, ["AAA", "BBB", "CCC", "NEW"], END, 60)
    assert list(res.prices.columns) == ["AAA", "BBB", "CCC"]
    (q,) = res.dropped
    assert q.symbol == "NEW"
    assert q.status == "dropped_short_history"
    assert q.bars_available == 30
    assert q.source == "csv"


def test_symbol_without_bars_is_dropped(sample_bars: list[Bar]) -> None:
    res = build_price_matrix(sample_bars, ["AAA", "BBB", "CCC", "ZZZ"], END, 60)
    (q,) = res.dropped
    assert q.symbol == "ZZZ"
    assert q.status == "dropped_missing"
    assert q.bars_available == 0
    assert q.source == ""


def test_too_few_days_raises() -> None:
    bars = make_bars(["AAA", "BBB"], 10)
    with pytest.raises(AnalysisError):
        build_price_matrix(bars, ["AAA", "BBB"], END, 10)


def test_invalid_n_days_raises(sample_bars: list[Bar]) -> None:
    with pytest.raises(AnalysisError):
        build_price_matrix(sample_bars, ["AAA"], END, 0)


def test_log_returns_rows_and_values(sample_bars: list[Bar]) -> None:
    res = build_price_matrix(sample_bars, ["AAA", "BBB", "CCC"], END, 60)
    rets = log_returns(res.prices)
    assert rets.shape == (60, 3)
    assert rets.index[0] == res.prices.index[1]
    p = res.prices["AAA"].to_numpy()
    np.testing.assert_allclose(rets["AAA"].to_numpy(), np.log(p[1:] / p[:-1]))


def test_log_returns_of_constant_prices_is_zero() -> None:
    idx = pd.Index([date(2026, 1, d) for d in range(5, 10)], name="date")
    prices = pd.DataFrame({"X": [50.0] * 5, "Y": [7.0] * 5}, index=idx)
    rets = log_returns(prices)
    assert rets.shape == (4, 2)
    assert (rets.to_numpy() == 0.0).all()
