"""Inputs shared by the brief, the workbook and the Sheets publisher."""

from __future__ import annotations

from dataclasses import dataclass

from uncorrelated_trading.config import ReportConfig
from uncorrelated_trading.models import AnalysisResult, PortfolioContext


@dataclass(frozen=True, slots=True)
class BriefInputs:
    """Everything the report renderers need for one run."""

    result: AnalysisResult
    portfolio: PortfolioContext
    previous_largest: tuple[str, ...] | None  # for the "changes since last run" line
    report_cfg: ReportConfig
    app_version: str


def holdings_rows(inputs: BriefInputs) -> list[dict[str, object]]:
    """Join current positions with momentum and largest-set membership.

    Parameters
    ----------
    inputs : BriefInputs
        Report inputs.

    Returns
    -------
    list[dict[str, object]]
        One row per position, sorted by symbol, with keys ``symbol``, ``qty``,
        ``current_price``, ``market_value``, ``unrealized_pl``, ``unrealized_plpc``,
        ``momentum_rank`` (``None`` when the symbol was not analysed), ``direction``
        (``""`` when not analysed) and ``in_largest_set`` (bool).
    """
    momentum = {m.symbol: m for m in inputs.result.momentum}
    largest = set(inputs.result.largest_set.symbols) if inputs.result.largest_set else set()
    rows: list[dict[str, object]] = []
    for p in sorted(inputs.portfolio.positions, key=lambda x: x.symbol):
        m = momentum.get(p.symbol)
        rows.append(
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "current_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_plpc": p.unrealized_plpc,
                "momentum_rank": m.rank if m else None,
                "direction": m.direction if m else "",
                "in_largest_set": p.symbol in largest,
            }
        )
    return rows
