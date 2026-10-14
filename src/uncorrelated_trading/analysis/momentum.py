"""Composite momentum scores from a matrix of daily log returns.

For each symbol three components are computed from the trailing rows of the
return matrix ``R`` (dates ascending, one column per symbol):

* ``trailing``: sum of the last ``k`` log returns;
* ``skip``: sum of log returns over ``[t-L, t-s)`` (a 12-1 style window that
  omits the most recent ``s`` days);
* ``vol_adj``: ``trailing / (sigma * sqrt(k))`` with ``sigma`` the sample
  standard deviation (``ddof=1``) of the last ``k`` returns, and 0 when
  ``sigma`` is 0.

Each component is z-scored across symbols (population std, 0 when the std is
0), winsorised to ``[-3, 3]``, and blended as ``w . z / sum(w)``. Symbols are
ranked by composite descending (rank 1 = strongest; ties broken by symbol
name) and labelled ``up``/``down``/``flat`` against ``direction_threshold``.
The output is advisory only.
"""

from __future__ import annotations

# ============================================================
# IMPORTS
# ============================================================
import math

import numpy as np
import pandas as pd

from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import AnalysisParams, Direction, MomentumScore

# ============================================================
# CONSTANTS
# ============================================================

TRADING_DAYS_PER_YEAR = 252
WINSOR_LIMIT = 3.0
# A standard deviation below this is treated as exactly zero, so that a column
# of identical values (whose floating-point std may be ~1e-19) scores 0.
ZERO_STD_TOL = 1e-12

# ============================================================
# PUBLIC API
# ============================================================


def zscore(x: pd.Series) -> pd.Series:
    """Z-score a series with the population std; all zeros when the std is 0.

    Parameters
    ----------
    x : pd.Series
        Finite values to standardise.

    Returns
    -------
    pd.Series
        ``(x - mean) / std(ddof=0)`` with the same index as ``x``.
    """
    values = x.to_numpy(dtype=float)
    if len(values) == 0:
        return pd.Series(np.zeros(0), index=x.index, dtype=float)
    std = float(np.std(values, ddof=0))
    if not math.isfinite(std) or std < ZERO_STD_TOL:
        return pd.Series(np.zeros(len(values)), index=x.index, dtype=float)
    return pd.Series((values - float(np.mean(values))) / std, index=x.index, dtype=float)


def momentum_scores(returns: pd.DataFrame, params: AnalysisParams) -> tuple[MomentumScore, ...]:
    """Compute composite momentum scores for every column of ``returns``.

    Parameters
    ----------
    returns : pd.DataFrame
        Daily log returns, index dates ascending, one column per symbol.
    params : AnalysisParams
        Uses ``momentum_k``, ``momentum_long``, ``momentum_skip``,
        ``momentum_weights`` and ``direction_threshold``.

    Returns
    -------
    tuple[MomentumScore, ...]
        One score per symbol, sorted by rank ascending (1 = strongest).

    Raises
    ------
    AnalysisError
        If there are fewer than ``momentum_long`` rows or fewer than 2
        symbols, if the parameters are inconsistent, or if the trailing
        ``momentum_long`` rows contain non-finite values.
    """
    _validate(returns, params)
    k, long_, skip = params.momentum_k, params.momentum_long, params.momentum_skip
    n_rows = len(returns)
    values = returns.to_numpy(dtype=float)
    symbols = [str(c) for c in returns.columns]

    # ----- raw components per symbol -----
    recent = values[n_rows - k :]
    trailing = recent.sum(axis=0)
    skip_ret = values[n_rows - long_ : n_rows - skip].sum(axis=0)
    sigma = recent.std(axis=0, ddof=1)
    sigma = np.where(sigma < ZERO_STD_TOL, 0.0, sigma)
    safe_sigma = np.where(sigma == 0.0, 1.0, sigma)
    vol_adj = np.where(sigma == 0.0, 0.0, trailing / (safe_sigma * math.sqrt(k)))
    realized_vol = sigma * math.sqrt(TRADING_DAYS_PER_YEAR)

    # ----- cross-sectional z-scores and weighted blend -----
    composite = _composite(symbols, (trailing, skip_ret, vol_adj), params.momentum_weights)

    # ----- dense ranks, ties broken by symbol name -----
    order = sorted(range(len(symbols)), key=lambda i: (-composite[i], symbols[i]))
    scores = tuple(
        MomentumScore(
            symbol=symbols[i],
            trailing_return=float(trailing[i]),
            skip_return=float(skip_ret[i]),
            vol_adjusted_return=float(vol_adj[i]),
            realized_vol=float(realized_vol[i]),
            composite_z=float(composite[i]),
            rank=pos + 1,
            direction=_direction(float(composite[i]), params.direction_threshold),
        )
        for pos, i in enumerate(order)
    )
    if len(scores) != len(symbols):
        raise AnalysisError("momentum scoring lost symbols")
    return scores


# ============================================================
# PRIVATE HELPERS
# ============================================================


def _validate(returns: pd.DataFrame, params: AnalysisParams) -> None:
    """Raise ``AnalysisError`` if ``returns`` or ``params`` cannot be scored."""
    k, long_, skip = params.momentum_k, params.momentum_long, params.momentum_skip
    if returns.shape[1] < 2:
        raise AnalysisError(f"momentum needs at least 2 symbols, got {returns.shape[1]}")
    if len(returns) < long_:
        raise AnalysisError(f"momentum needs at least {long_} rows of returns, got {len(returns)}")
    if not 2 <= k <= long_:
        raise AnalysisError(f"momentum_k must be in [2, {long_}], got {k}")
    if not 0 <= skip < long_:
        raise AnalysisError(f"momentum_skip must be in [0, {long_}), got {skip}")
    weights = params.momentum_weights
    if len(weights) != 3 or any(w < 0 for w in weights) or sum(weights) <= 0:
        raise AnalysisError(f"momentum_weights must be 3 non-negative values, got {weights}")
    window = returns.to_numpy(dtype=float)[len(returns) - long_ :]
    if not np.isfinite(window).all():
        raise AnalysisError("momentum window contains non-finite returns")


def _composite(
    symbols: list[str],
    components: tuple[np.ndarray, np.ndarray, np.ndarray],
    weights: tuple[float, float, float],
) -> np.ndarray:
    """Blend winsorised cross-sectional z-scores of the three components.

    Parameters
    ----------
    symbols : list[str]
        Column labels, used as the z-score index.
    components : tuple of np.ndarray
        Trailing, skip and vol-adjusted returns, one value per symbol.
    weights : tuple[float, float, float]
        Non-negative blend weights with a positive sum.

    Returns
    -------
    np.ndarray
        ``w . z / sum(w)`` per symbol.
    """
    total = np.zeros(len(symbols), dtype=float)
    for weight, component in zip(weights, components, strict=True):
        z = zscore(pd.Series(component, index=symbols)).to_numpy()
        total += weight * np.clip(z, -WINSOR_LIMIT, WINSOR_LIMIT)
    return total / float(sum(weights))


def _direction(composite: float, threshold: float) -> Direction:
    """Map a composite z-score to ``up``, ``down`` or ``flat``."""
    if composite > threshold:
        return "up"
    if composite < -threshold:
        return "down"
    return "flat"
