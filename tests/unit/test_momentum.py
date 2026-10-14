"""Unit tests for composite momentum scoring."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from uncorrelated_trading.analysis.momentum import momentum_scores, zscore
from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import AnalysisParams


def make_params(**overrides: object) -> AnalysisParams:
    base: dict[str, object] = {
        "primary_window": 60,
        "windows": (60, 120, 250),
        "tau": 0.20,
        "tau_spearman": 0.30,
        "max_set_size_exact": 4,
        "max_sets_per_size": 50,
        "search_time_budget_s": 20.0,
        "momentum_k": 20,
        "momentum_long": 120,
        "momentum_skip": 20,
        "momentum_weights": (0.4, 0.3, 0.3),
        "direction_threshold": 0.5,
    }
    base.update(overrides)
    return AnalysisParams(**base)  # type: ignore[arg-type]


def frame(columns: dict[str, np.ndarray]) -> pd.DataFrame:
    n = len(next(iter(columns.values())))
    idx = pd.bdate_range("2026-01-02", periods=n)
    return pd.DataFrame(columns, index=idx)


def noisy(n: int, seed: int, mu: float = 0.0, sd: float = 0.01) -> np.ndarray:
    return np.random.default_rng(seed).normal(mu, sd, size=n)


def test_monotone_trend_ranks_first() -> None:
    n = 150
    df = frame(
        {
            "FLAT1": np.zeros(n),
            "FLAT2": np.zeros(n),
            "TREND": np.full(n, 0.002) + noisy(n, 1, sd=0.0005),
            "FLAT3": np.zeros(n),
        }
    )
    scores = momentum_scores(df, make_params())
    assert scores[0].symbol == "TREND"
    assert scores[0].rank == 1
    assert scores[0].direction == "up"
    assert scores[0].composite_z > 0
    # flat symbols tie and are ordered by name
    assert [s.symbol for s in scores[1:]] == ["FLAT1", "FLAT2", "FLAT3"]


def test_trailing_and_skip_returns_match_definition() -> None:
    n = 130
    df = frame({"A": noisy(n, 2), "B": noisy(n, 3), "C": noisy(n, 4)})
    params = make_params()
    by_sym = {s.symbol: s for s in momentum_scores(df, params)}
    for sym in df.columns:
        r = df[sym].to_numpy()
        sigma = float(np.std(r[-20:], ddof=1))
        assert by_sym[sym].trailing_return == pytest.approx(r[-20:].sum())
        assert by_sym[sym].skip_return == pytest.approx(r[-120:-20].sum())
        assert by_sym[sym].realized_vol == pytest.approx(sigma * math.sqrt(252))
        assert by_sym[sym].vol_adjusted_return == pytest.approx(
            r[-20:].sum() / (sigma * math.sqrt(20))
        )


def test_direction_thresholds() -> None:
    n = 130
    df = frame(
        {
            "UP": np.full(n, 0.003) + noisy(n, 5, sd=0.0005),
            "MID": noisy(n, 6, sd=0.0005),
            "DOWN": np.full(n, -0.003) + noisy(n, 7, sd=0.0005),
        }
    )
    params = make_params(direction_threshold=0.5)
    by_sym = {s.symbol: s for s in momentum_scores(df, params)}
    for s in by_sym.values():
        if s.composite_z > 0.5:
            assert s.direction == "up"
        elif s.composite_z < -0.5:
            assert s.direction == "down"
        else:
            assert s.direction == "flat"
    assert by_sym["UP"].direction == "up"
    assert by_sym["DOWN"].direction == "down"
    assert by_sym["MID"].direction == "flat"
    # a huge threshold makes everything flat
    huge = momentum_scores(df, make_params(direction_threshold=10.0))
    assert all(s.direction == "flat" for s in huge)


def test_vol_adjusted_zero_when_sigma_zero() -> None:
    n = 130
    df = frame({"ZERO": np.zeros(n), "CONST": np.full(n, 0.001), "B": noisy(n, 8)})
    by_sym = {s.symbol: s for s in momentum_scores(df, make_params())}
    assert by_sym["ZERO"].vol_adjusted_return == 0.0
    assert by_sym["ZERO"].realized_vol == 0.0
    assert by_sym["CONST"].vol_adjusted_return == 0.0
    assert by_sym["CONST"].realized_vol == pytest.approx(0.0, abs=1e-12)
    assert by_sym["CONST"].trailing_return == pytest.approx(0.02)


def test_requires_long_window() -> None:
    df = frame({"A": noisy(119, 9), "B": noisy(119, 10)})
    with pytest.raises(AnalysisError):
        momentum_scores(df, make_params())
    # exactly momentum_long rows is enough
    ok = frame({"A": noisy(120, 9), "B": noisy(120, 10)})
    assert len(momentum_scores(ok, make_params())) == 2


def test_requires_two_symbols() -> None:
    with pytest.raises(AnalysisError):
        momentum_scores(frame({"A": noisy(150, 11)}), make_params())


def test_non_finite_window_rejected() -> None:
    a = noisy(150, 12)
    a[-5] = np.nan
    with pytest.raises(AnalysisError):
        momentum_scores(frame({"A": a, "B": noisy(150, 13)}), make_params())


def test_rank_is_dense_and_starts_at_one() -> None:
    n = 140
    df = frame({f"S{i}": noisy(n, 20 + i, mu=0.0005 * i) for i in range(6)})
    scores = momentum_scores(df, make_params())
    assert [s.rank for s in scores] == list(range(1, 7))
    composites = [s.composite_z for s in scores]
    assert composites == sorted(composites, reverse=True)
    assert sorted(s.symbol for s in scores) == sorted(df.columns)


def test_zscore_population_std_and_zero_case() -> None:
    z = zscore(pd.Series([1.0, 2.0, 3.0], index=["a", "b", "c"]))
    expected = (np.array([1.0, 2.0, 3.0]) - 2.0) / np.std([1.0, 2.0, 3.0], ddof=0)
    np.testing.assert_allclose(z.to_numpy(), expected)
    assert list(z.index) == ["a", "b", "c"]
    zeros = zscore(pd.Series([4.0, 4.0, 4.0]))
    assert (zeros == 0.0).all()


def test_composite_is_weighted_and_winsorised() -> None:
    n = 130
    # one extreme outlier among many near-identical symbols gives a raw z > 3
    cols = {f"S{i:02d}": noisy(n, 40 + i, sd=0.0001) for i in range(15)}
    cols["OUT"] = np.full(n, 0.05) + noisy(n, 99, sd=0.0001)
    scores = momentum_scores(frame(cols), make_params(momentum_weights=(1.0, 0.0, 0.0)))
    assert scores[0].symbol == "OUT"
    assert scores[0].composite_z == pytest.approx(3.0)
    assert all(-3.0 <= s.composite_z <= 3.0 for s in scores)
