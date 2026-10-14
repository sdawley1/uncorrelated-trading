"""Shared frozen data contracts used by every layer of uncorrelated_trading.

All classes are ``@dataclass(frozen=True, slots=True)``. ``CorrelationMatrix`` also sets
``eq=False`` because it holds a numpy array; compare instances with
:func:`correlation_matrices_equal`. Money and prices are ``float``; dates are
``datetime.date``; instants are tz-aware ``datetime`` in UTC. This module is frozen after
Task 1: changes go through the lead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import numpy as np

AsOfMode = Literal["pre_open", "intraday", "post_close_settling", "post_close", "non_trading_day"]
Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily OHLCV bar for a symbol on an exchange-local trading date."""

    symbol: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str  # "alpaca" | "twelvedata" | "csv"


@dataclass(frozen=True, slots=True)
class TradingDay:
    """A trading session with its open and close instants in UTC."""

    day: date
    open_utc: datetime
    close_utc: datetime


@dataclass(frozen=True, slots=True)
class AsOf:
    """Resolved as-of trading day for a run, with the mode that produced it."""

    day: date  # last trading day whose close is included
    mode: AsOfMode
    now_utc: datetime
    provisional: bool  # True in post_close_settling: today's bars may still be revised
    calendar_source: Literal["alpaca", "static"]
    note: str  # human-readable reason


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Brokerage account balances read at ``taken_at`` (read-only; no order code)."""

    taken_at: datetime
    cash: float
    equity: float
    buying_power: float
    portfolio_value: float
    last_equity: float
    currency: str
    paper: bool


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """One open position as reported by the broker."""

    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float
    change_today: float


@dataclass(frozen=True, slots=True, eq=False)
class CorrelationMatrix:
    """Correlation matrix of daily log returns over one window (compare via helper)."""

    window: int  # trading days of returns used
    method: Literal["pearson", "spearman"]
    symbols: tuple[str, ...]
    values: np.ndarray  # shape (n, n), symmetric, unit diagonal
    n_obs: int
    start_day: date
    end_day: date


@dataclass(frozen=True, slots=True)
class UncorrelatedSet:
    """A set of symbols that are pairwise uncorrelated, with diagnostics."""

    symbols: tuple[str, ...]  # sorted
    size: int
    max_abs_rho: float  # Pearson, primary window
    mean_abs_rho: float
    max_abs_rho_ci_upper: float  # Fisher-z 95 % upper bound on the largest |rho| in the set
    min_eigenvalue: float  # of Pearson sub-matrix
    spearman_max_abs_rho: float
    passes_spearman: bool  # spearman_max_abs_rho < tau_spearman
    stable_in_long_window: bool  # also pairwise below tau in the longest configured window
    mean_momentum_z: float  # mean composite momentum of members (filled by pipeline)


@dataclass(frozen=True, slots=True)
class MomentumScore:
    """Composite momentum components and rank for one symbol."""

    symbol: str
    trailing_return: float  # log return over k days
    skip_return: float  # log return over [t-L, t-s]
    vol_adjusted_return: float  # trailing_return / (sigma_daily * sqrt(k))
    realized_vol: float  # annualised, sqrt(252) * sigma_daily
    composite_z: float
    rank: int  # 1 = strongest
    direction: Direction


@dataclass(frozen=True, slots=True)
class SymbolQuality:
    """Data-quality outcome for one requested symbol."""

    symbol: str
    status: Literal[
        "ok", "dropped_missing", "dropped_short_history", "fallback_used", "close_mismatch"
    ]
    detail: str
    bars_available: int
    source: str


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """Data-quality summary for a run: requested vs analysed symbols and source usage."""

    requested: tuple[str, ...]
    analysed: tuple[str, ...]
    per_symbol: tuple[SymbolQuality, ...]
    primary_source_failures: int
    fallback_calls: int


@dataclass(frozen=True, slots=True)
class AnalysisParams:
    """Analysis parameters (defaults shown in comments come from ``config/default.toml``)."""

    primary_window: int  # 60
    windows: tuple[int, ...]  # (60, 120, 250)
    tau: float  # 0.20
    tau_spearman: float  # 0.30
    max_set_size_exact: int  # 4
    max_sets_per_size: int  # 50
    search_time_budget_s: float  # 20.0
    momentum_k: int  # 20
    momentum_long: int  # 120
    momentum_skip: int  # 20
    momentum_weights: tuple[float, float, float]  # (0.4, 0.3, 0.3)
    direction_threshold: float  # 0.5


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Complete output of one analysis run."""

    run_id: str  # uuid4 hex
    as_of: AsOf
    created_at: datetime
    params: AnalysisParams
    universe: tuple[str, ...]
    correlations: tuple[CorrelationMatrix, ...]  # pearson+spearman per window
    pairs: tuple[UncorrelatedSet, ...]  # size 2, ascending max_abs_rho
    sets_by_size: dict[int, tuple[UncorrelatedSet, ...]]  # 3..max_set_size_exact
    largest_set: UncorrelatedSet | None
    largest_set_exact: bool  # False if time budget hit
    momentum: tuple[MomentumScore, ...]  # sorted by rank
    quality: DataQualityReport


@dataclass(frozen=True, slots=True)
class PortfolioContext:
    """Account and positions for the report, or the error that prevented reading them."""

    account: AccountSnapshot | None
    positions: tuple[PositionSnapshot, ...]
    error: str | None  # set when account read failed


@dataclass(frozen=True, slots=True)
class Attachment:
    """An email attachment."""

    filename: str
    content: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A backend-neutral email message."""

    sender: str
    recipients: tuple[str, ...]
    subject: str
    text_body: str
    html_body: str
    attachments: tuple[Attachment, ...]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Confirmation returned by an email backend after a successful send."""

    backend: str
    message_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class ReportBundle:
    """Everything needed to deliver one report."""

    result: AnalysisResult
    portfolio: PortfolioContext
    message: EmailMessage
    sheets_url: str | None


def correlation_matrices_equal(
    a: CorrelationMatrix, b: CorrelationMatrix, atol: float = 1e-12
) -> bool:
    """Compare two correlation matrices field by field.

    Parameters
    ----------
    a, b : CorrelationMatrix
        Matrices to compare.
    atol : float
        Absolute tolerance for the ``values`` arrays (``np.allclose`` with ``rtol=0``).

    Returns
    -------
    bool
        True when all non-array fields are equal and ``values`` have the same shape and agree
        element-wise within ``atol``.
    """
    if (a.window, a.method, a.symbols, a.n_obs, a.start_day, a.end_day) != (
        b.window,
        b.method,
        b.symbols,
        b.n_obs,
        b.start_day,
        b.end_day,
    ):
        return False
    if a.values.shape != b.values.shape:
        return False
    return bool(np.allclose(a.values, b.values, rtol=0.0, atol=atol))
