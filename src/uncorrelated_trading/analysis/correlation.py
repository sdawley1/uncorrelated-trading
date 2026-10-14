"""Correlation matrices of daily log returns over a trailing window.

``correlation_matrix`` wraps ``DataFrame.corr`` and enforces the invariants downstream code
relies on: exact symmetry, an exact unit diagonal, and entries clipped to [-1, 1].
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd

from uncorrelated_trading.errors import AnalysisError
from uncorrelated_trading.models import CorrelationMatrix

__all__ = ["correlation_matrix"]


def _as_date(value: object) -> date:
    """Convert an index label (``date``, ``datetime`` or ``Timestamp``) to ``date``."""
    if isinstance(value, datetime):  # includes pandas.Timestamp
        return value.date()
    if isinstance(value, date):
        return value
    raise AnalysisError(f"returns index label {value!r} is not a date")


def correlation_matrix(
    returns: pd.DataFrame, method: Literal["pearson", "spearman"], window: int
) -> CorrelationMatrix:
    """Correlation matrix of the last ``window`` rows of ``returns``.

    Parameters
    ----------
    returns : pandas.DataFrame
        Daily log returns; index ascending by day, columns symbols.
    method : {"pearson", "spearman"}
        Correlation estimator passed to ``DataFrame.corr``.
    window : int
        Number of trailing rows to use.

    Returns
    -------
    CorrelationMatrix
        ``values`` is ``(C + C.T) / 2`` with the diagonal set to exactly 1.0 and entries
        clipped to [-1, 1]. Pairs with an undefined correlation (a zero-variance series) are
        set to 0.0. ``n_obs`` equals ``window``; ``start_day``/``end_day`` are the first
        and last index labels used.

    Raises
    ------
    AnalysisError
        If ``window < 2``, ``returns`` has fewer than ``window`` rows, or fewer than two
        columns.
    """
    if window < 2:
        raise AnalysisError(f"window must be >= 2, got {window}")
    n_rows, n_cols = returns.shape
    if n_rows < window:
        raise AnalysisError(f"need {window} return rows, have {n_rows}")
    if n_cols < 2:
        raise AnalysisError(f"need at least 2 symbols for a correlation matrix, have {n_cols}")
    used = returns.iloc[-window:].astype(np.float64)
    raw = used.corr(method=method).to_numpy(dtype=np.float64)
    values = (raw + raw.T) / 2.0
    values = np.nan_to_num(values, nan=0.0)
    values = np.clip(values, -1.0, 1.0)
    np.fill_diagonal(values, 1.0)
    return CorrelationMatrix(
        window=window,
        method=method,
        symbols=tuple(str(c) for c in used.columns),
        values=values,
        n_obs=window,
        start_day=_as_date(used.index[0]),
        end_day=_as_date(used.index[-1]),
    )
