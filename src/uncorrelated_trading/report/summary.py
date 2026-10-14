"""Text and HTML rendering of the daily brief.

The brief is built once as a list of sections (paragraphs and tables) and then rendered
either as plain text (lines of at most 100 characters) or as a single self-contained HTML
document with inline CSS and ``<table>`` elements (no external URLs, no scripts). Rendering
is deterministic given its inputs: the only timestamp used is ``result.created_at``.

Section order follows spec A9: header, data quality, top uncorrelated pairs, best sets by
size, largest set, momentum top/bottom, holdings, account summary, parameters footer. The
output is advisory only.
"""

from __future__ import annotations

# ============================================================
# IMPORTS
# ============================================================
import html
import math
import textwrap
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from uncorrelated_trading.models import (
    AnalysisResult,
    MomentumScore,
    PortfolioContext,
    UncorrelatedSet,
)
from uncorrelated_trading.report.inputs import BriefInputs, holdings_rows

__all__ = ["BriefInputs", "holdings_rows", "render_html", "render_text", "subject_line"]

# ============================================================
# CONSTANTS
# ============================================================

TEXT_WIDTH = 100
SEND_TZ = ZoneInfo("America/New_York")
MIN_SET_SIZE = 3
TIME_CAPPED_LABEL = "(search time-capped; may not be maximal)"
PROVISIONAL_NOTE = "*** PROVISIONAL: data for {day} may still be revised by the data provider ***"
DISCLAIMER = "Advisory output, not investment advice."

_CSS = (
    "body{font-family:Helvetica,Arial,sans-serif;font-size:14px;color:#222;"
    "background:#fff;margin:16px;}"
    "h1{font-size:18px;margin:0 0 8px 0;}"
    "h2{font-size:15px;margin:20px 0 6px 0;border-bottom:1px solid #ccc;}"
    "p{margin:4px 0;}"
    "table{border-collapse:collapse;margin:6px 0;}"
    "th,td{border:1px solid #ddd;padding:3px 8px;font-size:13px;}"
    "th{background:#f2f2f2;text-align:left;}"
    "td.num{text-align:right;font-family:Menlo,Consolas,monospace;}"
    ".warn{color:#a00;font-weight:bold;}"
    ".foot{color:#555;font-size:12px;}"
)

# ============================================================
# INTERNAL DOCUMENT MODEL
# ============================================================


@dataclass(frozen=True, slots=True)
class _Para:
    """A paragraph; ``warn`` renders emphasised."""

    text: str
    warn: bool = False


@dataclass(frozen=True, slots=True)
class _Table:
    """A table; ``numeric`` flags right-aligned columns."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    numeric: tuple[bool, ...]


_Block = _Para | _Table


@dataclass(frozen=True, slots=True)
class _Section:
    """A titled section of blocks; ``footer`` sections render in small print."""

    title: str
    blocks: tuple[_Block, ...]
    footer: bool = False


# ============================================================
# FORMATTING HELPERS
# ============================================================


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _pct(x: float) -> str:
    return f"{x:.2%}"


def _rho(x: float) -> str:
    return f"{x:.3f}"


def _qty(x: float) -> str:
    return f"{x:,.0f}" if x == int(x) else f"{x:,.4f}".rstrip("0")


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _send_day(result: AnalysisResult) -> str:
    return result.created_at.astimezone(SEND_TZ).date().isoformat()


def _primary_n_obs(result: AnalysisResult) -> int:
    for cm in result.correlations:
        if cm.method == "pearson" and cm.window == result.params.primary_window:
            return cm.n_obs
    return result.params.primary_window


def _long_window(result: AnalysisResult) -> int:
    return max(result.params.windows) if result.params.windows else result.params.primary_window


# ============================================================
# SECTION BUILDERS
# ============================================================


def _changes_line(inputs: BriefInputs) -> str:
    prev = inputs.previous_largest
    if prev is None:
        return "Changes since last run: no previous run"
    largest = inputs.result.largest_set
    current = set(largest.symbols) if largest else set()
    added = sorted(current - set(prev))
    removed = sorted(set(prev) - current)
    if not added and not removed:
        return "Changes since last run: largest set unchanged"
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return "Changes since last run: largest set " + "; ".join(parts)


def _header(inputs: BriefInputs) -> _Section:
    r = inputs.result
    blocks: list[_Block] = []
    if r.as_of.provisional:
        blocks.append(_Para(PROVISIONAL_NOTE.format(day=r.as_of.day.isoformat()), warn=True))
    blocks += [
        _Para(
            f"Data through {r.as_of.day.isoformat()} (mode: {r.as_of.mode}; "
            f"calendar: {r.as_of.calendar_source})."
        ),
        _Para(f"Note: {r.as_of.note}"),
        _Para(f"Universe: {len(r.universe)} symbols; analysed: {len(r.quality.analysed)} symbols."),
        _Para(_changes_line(inputs)),
    ]
    return _Section(subject_line(inputs), tuple(blocks))


def _quality(result: AnalysisResult) -> _Section:
    q = result.quality
    blocks: list[_Block] = [
        _Para(
            f"{len(q.analysed)} of {len(q.requested)} requested symbols analysed; "
            f"{q.primary_source_failures} primary-source failures; "
            f"{q.fallback_calls} fallback calls."
        )
    ]
    dropped = [s for s in q.per_symbol if s.status.startswith("dropped")]
    other = [s for s in q.per_symbol if s.status != "ok" and not s.status.startswith("dropped")]
    if dropped:
        blocks.append(
            _Para("Dropped: " + "; ".join(f"{s.symbol} ({s.status}: {s.detail})" for s in dropped))
        )
    else:
        blocks.append(_Para("Dropped: none"))
    if other:
        blocks.append(
            _Para("Notes: " + "; ".join(f"{s.symbol} ({s.status}: {s.detail})" for s in other))
        )
    return _Section("Data quality", tuple(blocks))


def _set_table(sets: tuple[UncorrelatedSet, ...], label: str) -> _Table:
    rows = tuple(
        (
            str(i),
            ", ".join(s.symbols),
            _rho(s.max_abs_rho),
            _rho(s.max_abs_rho_ci_upper),
            _rho(s.mean_abs_rho),
            _rho(s.spearman_max_abs_rho),
            _yes(s.stable_in_long_window),
            f"{s.mean_momentum_z:+.2f}",
        )
        for i, s in enumerate(sets, start=1)
    )
    return _Table(
        ("#", label, "max|rho|", "CI upper", "mean|rho|", "Spearman", "Stable", "Mom z"),
        rows,
        (True, False, True, True, True, True, False, True),
    )


def _pairs(inputs: BriefInputs) -> _Section:
    r = inputs.result
    title = (
        f"Top uncorrelated pairs (|rho| < {r.params.tau:.2f}, "
        f"{r.params.primary_window}-day Pearson)"
    )
    pairs = r.pairs[: inputs.report_cfg.top_pairs]
    if not pairs:
        return _Section(title, (_Para("none found"),))
    return _Section(title, (_set_table(pairs, "Pair"),))


def _sets(inputs: BriefInputs) -> _Section:
    r = inputs.result
    blocks: list[_Block] = []
    for size in range(MIN_SET_SIZE, r.params.max_set_size_exact + 1):
        sets = r.sets_by_size.get(size, ())[: inputs.report_cfg.top_sets_per_size]
        if not sets:
            blocks.append(_Para(f"Size {size}: none found"))
        else:
            blocks.append(_Para(f"Size {size}:"))
            blocks.append(_set_table(sets, "Symbols"))
    return _Section("Best sets by size", tuple(blocks))


def _largest(result: AnalysisResult) -> _Section:
    s = result.largest_set
    if s is None:
        return _Section("Largest set", (_Para("none found"),))
    flag = "(exact search)" if result.largest_set_exact else TIME_CAPPED_LABEL
    long_w = _long_window(result)
    spearman = "passes" if s.passes_spearman else "fails"
    blocks: tuple[_Block, ...] = (
        _Para(f"Size {s.size} {flag}"),
        _Para("Symbols: " + ", ".join(s.symbols)),
        _Table(
            ("Diagnostic", "Value"),
            (
                ("max |rho|", _rho(s.max_abs_rho)),
                ("95% CI upper of max |rho|", _rho(s.max_abs_rho_ci_upper)),
                ("mean |rho|", _rho(s.mean_abs_rho)),
                ("min eigenvalue", _rho(s.min_eigenvalue)),
                (
                    f"Spearman max |rho| (tau {result.params.tau_spearman:.2f})",
                    f"{_rho(s.spearman_max_abs_rho)} ({spearman})",
                ),
                (f"stable in {long_w}-day window", _yes(s.stable_in_long_window)),
                ("mean momentum z", f"{s.mean_momentum_z:+.2f}"),
            ),
            (False, True),
        ),
    )
    return _Section("Largest set", blocks)


def _momentum_table(scores: tuple[MomentumScore, ...], k: int) -> _Table:
    rows = tuple(
        (
            str(m.rank),
            m.symbol,
            f"{m.composite_z:+.2f}",
            _pct(m.trailing_return),
            _pct(m.skip_return),
            f"{m.vol_adjusted_return:+.2f}",
            _pct(m.realized_vol),
            m.direction,
        )
        for m in scores
    )
    return _Table(
        ("Rank", "Symbol", "Comp z", f"{k}d log ret", "Skip log ret", "Vol-adj", "Ann vol", "Dir"),
        rows,
        (True, False, True, True, True, True, True, False),
    )


def _momentum(inputs: BriefInputs) -> _Section:
    r = inputs.result
    n = inputs.report_cfg.top_momentum
    if not r.momentum:
        return _Section("Momentum", (_Para("none computed"),))
    top = r.momentum[:n]
    bottom = r.momentum[n:][-n:]
    k = r.params.momentum_k
    blocks: list[_Block] = [
        _Para(f"Top {len(top)} by composite momentum:"),
        _momentum_table(top, k),
    ]
    if bottom:
        blocks += [_Para(f"Bottom {len(bottom)}:"), _momentum_table(bottom, k)]
    return _Section("Momentum", tuple(blocks))


def _holdings(inputs: BriefInputs) -> _Section:
    pf: PortfolioContext = inputs.portfolio
    if pf.error is not None:
        return _Section("Holdings", (_Para(f"Account unavailable: {pf.error}", warn=True),))
    rows = holdings_rows(inputs)
    if not rows:
        return _Section("Holdings", (_Para("No open positions."),))
    table_rows: list[tuple[str, ...]] = []
    for row in rows:
        rank = row["momentum_rank"]
        table_rows.append(
            (
                str(row["symbol"]),
                _qty(float(str(row["qty"]))),
                _money(float(str(row["current_price"]))),
                _money(float(str(row["market_value"]))),
                _money(float(str(row["unrealized_pl"]))),
                _pct(float(str(row["unrealized_plpc"]))),
                "-" if rank is None else str(rank),
                str(row["direction"]) or "-",
                _yes(bool(row["in_largest_set"])),
            )
        )
    return _Section(
        "Holdings",
        (
            _Table(
                ("Symbol", "Qty", "Price", "Value", "P&L", "P&L %", "Mom rank", "Dir", "In set"),
                tuple(table_rows),
                (False, True, True, True, True, True, True, False, False),
            ),
        ),
    )


def _account(inputs: BriefInputs) -> _Section:
    pf = inputs.portfolio
    if pf.error is not None or pf.account is None:
        msg = pf.error if pf.error is not None else "no account data"
        return _Section("Account summary", (_Para(f"Account unavailable: {msg}", warn=True),))
    a = pf.account
    day_change = a.equity - a.last_equity
    day_pct = day_change / a.last_equity if a.last_equity else 0.0
    return _Section(
        "Account summary",
        (
            _Table(
                ("Item", f"Value ({a.currency})"),
                (
                    ("Account type", "paper" if a.paper else "live"),
                    ("Equity", _money(a.equity)),
                    ("Change since previous close", f"{_money(day_change)} ({_pct(day_pct)})"),
                    ("Cash", _money(a.cash)),
                    ("Buying power", _money(a.buying_power)),
                    ("Portfolio value", _money(a.portfolio_value)),
                ),
                (False, True),
            ),
        ),
    )


def _footer(inputs: BriefInputs) -> _Section:
    r = inputs.result
    p = r.params
    n_obs = _primary_n_obs(r)
    windows = ", ".join(str(w) for w in p.windows)
    weights = ", ".join(f"{w:.2f}" for w in p.momentum_weights)
    blocks: list[_Block] = [
        _Para(
            f"Parameters: tau={p.tau:.2f}, tau_spearman={p.tau_spearman:.2f}, "
            f"primary window={p.primary_window}, windows=({windows}), "
            f"momentum k={p.momentum_k}, L={p.momentum_long}, s={p.momentum_skip}, "
            f"weights=({weights}), direction threshold={p.direction_threshold:.2f}."
        )
    ]
    if n_obs > 3:
        se = 1.0 / math.sqrt(n_obs - 3)
        blocks.append(
            _Para(
                f"Statistical caveat: SE(rho) ~ {se:.2f} near zero (1/sqrt(n-3) with "
                f"n = {n_obs} daily returns in the primary window), so tau = {p.tau:.2f} is "
                f"{p.tau / se:.2f} standard errors from zero; pairs below tau are not proven "
                "independent."
            )
        )
    else:
        blocks.append(
            _Para(
                f"Statistical caveat: only {n_obs} daily returns; correlation estimates are "
                "unreliable."
            )
        )
    blocks += [
        _Para(
            "Dividend caveat: prices are split-adjusted only, so returns (including the "
            f"{p.momentum_long}-day long-window momentum return) omit dividend yield."
        ),
        _Para(DISCLAIMER),
        _Para(f"uncorrelated-trading {inputs.app_version}; run_id {r.run_id}."),
    ]
    return _Section("Parameters and caveats", tuple(blocks), footer=True)


def _sections(inputs: BriefInputs) -> list[_Section]:
    return [
        _header(inputs),
        _quality(inputs.result),
        _pairs(inputs),
        _sets(inputs),
        _largest(inputs.result),
        _momentum(inputs),
        _holdings(inputs),
        _account(inputs),
        _footer(inputs),
    ]


# ============================================================
# RENDERERS
# ============================================================


def _text_table(t: _Table) -> list[str]:
    widths = [len(h) for h in t.headers]
    for row in t.rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]

    def fmt(cells: tuple[str, ...]) -> str:
        out = [
            c.rjust(w) if num else c.ljust(w)
            for c, w, num in zip(cells, widths, t.numeric, strict=True)
        ]
        return ("  " + "  ".join(out)).rstrip()

    lines = [fmt(t.headers), "  " + "  ".join("-" * w for w in widths)]
    lines += [fmt(row) for row in t.rows]
    return lines


def _text_block(b: _Block) -> list[str]:
    if isinstance(b, _Table):
        return _text_table(b)
    return textwrap.wrap(
        b.text,
        width=TEXT_WIDTH,
        subsequent_indent="    ",
        break_long_words=True,
        break_on_hyphens=False,
    )


def subject_line(inputs: BriefInputs) -> str:
    """Build the email subject line.

    Parameters
    ----------
    inputs : BriefInputs
        Report inputs.

    Returns
    -------
    str
        ``"{prefix} Brief for {send_day} (data through {as_of.day}, {mode})"`` where
        ``send_day`` is ``result.created_at`` as an America/New_York date.
    """
    r = inputs.result
    return (
        f"{inputs.report_cfg.subject_prefix} Brief for {_send_day(r)} "
        f"(data through {r.as_of.day.isoformat()}, {r.as_of.mode})"
    )


def render_text(inputs: BriefInputs) -> str:
    """Render the brief as plain text with lines of at most 100 characters.

    Parameters
    ----------
    inputs : BriefInputs
        Report inputs.

    Returns
    -------
    str
        The text brief, newline-terminated.
    """
    lines: list[str] = []
    for i, section in enumerate(_sections(inputs)):
        if i == 0:
            title = textwrap.wrap(section.title, width=TEXT_WIDTH)
            lines += title
            lines.append("=" * min(TEXT_WIDTH, max(len(t) for t in title)))
        else:
            lines += ["", section.title, "-" * min(TEXT_WIDTH, len(section.title))]
        for block in section.blocks:
            lines += _text_block(block)
    return "\n".join(lines) + "\n"


def _html_block(b: _Block) -> str:
    if isinstance(b, _Para):
        cls = ' class="warn"' if b.warn else ""
        return f"<p{cls}>{html.escape(b.text)}</p>"
    head = "".join(f"<th>{html.escape(h)}</th>" for h in b.headers)
    body = []
    for row in b.rows:
        cells = "".join(
            (f'<td class="num">{html.escape(c)}</td>' if num else f"<td>{html.escape(c)}</td>")
            for c, num in zip(row, b.numeric, strict=True)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(inputs: BriefInputs) -> str:
    """Render the brief as a self-contained HTML document.

    Parameters
    ----------
    inputs : BriefInputs
        Report inputs.

    Returns
    -------
    str
        One ``<html>`` document with inline CSS and ``<table>`` elements; no external URLs
        and no scripts.
    """
    sections = _sections(inputs)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(subject_line(inputs))}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
    ]
    for i, section in enumerate(sections):
        tag = "h1" if i == 0 else "h2"
        parts.append(f'<div class="{"foot" if section.footer else "section"}">')
        parts.append(f"<{tag}>{html.escape(section.title)}</{tag}>")
        parts += [_html_block(b) for b in section.blocks]
        parts.append("</div>")
    parts += ["</body>", "</html>"]
    return "\n".join(parts) + "\n"
