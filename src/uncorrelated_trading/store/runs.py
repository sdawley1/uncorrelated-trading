"""Persistence of analysis runs, portfolio snapshots, email log, job runs and API usage.

Floats that are NaN are stored by SQLite as NULL and come back as ``nan``. Correlation
matrices are stored as JSON nested lists; parameters and the data-quality report as JSON
objects. Entries of ``AnalysisResult.sets_by_size`` with an empty tuple are not representable
in the schema and are omitted on load.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import numpy as np

from uncorrelated_trading.clock import Clock, SystemClock
from uncorrelated_trading.errors import StoreError
from uncorrelated_trading.models import (
    AccountSnapshot,
    AnalysisParams,
    AnalysisResult,
    AsOf,
    AsOfMode,
    CorrelationMatrix,
    DataQualityReport,
    Direction,
    MomentumScore,
    PortfolioContext,
    PositionSnapshot,
    SendReceipt,
    SymbolQuality,
    UncorrelatedSet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SET_COLUMNS = (
    "run_id, size, rank, is_largest, symbols_json, max_abs_rho, mean_abs_rho,"
    " max_abs_rho_ci_upper, min_eigenvalue, spearman_max_abs_rho, passes_spearman,"
    " stable_in_long_window, mean_momentum_z"
)


def _iso(when: datetime) -> str:
    """Return a tz-aware datetime as an ISO 8601 UTC string with microseconds."""
    if when.tzinfo is None:
        msg = "datetime must be tz-aware"
        raise ValueError(msg)
    return when.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(value: str) -> datetime:
    """Parse an ISO 8601 string written by :func:`_iso`."""
    return datetime.fromisoformat(value)


def _reclaimable(row: sqlite3.Row, now: datetime, stale_after: timedelta | None) -> bool:
    """Return True when a ``job_runs`` row may be claimed again (``retry`` or stale ``running``)."""
    status = str(row["status"])
    if status == "retry":
        return True
    if status != "running" or stale_after is None:
        return False
    return now - _dt(str(row["started_at"])) >= stale_after


def _f(value: float | None) -> float:
    """Map a stored REAL (NULL for NaN) back to ``float``."""
    return math.nan if value is None else float(value)


def _set_row(run_id: str, s: UncorrelatedSet, rank: int, *, is_largest: bool) -> tuple[object, ...]:
    """Build an ``uncorrelated_sets`` row for ``s``."""
    return (
        run_id,
        s.size,
        rank,
        int(is_largest),
        json.dumps(list(s.symbols)),
        s.max_abs_rho,
        s.mean_abs_rho,
        s.max_abs_rho_ci_upper,
        s.min_eigenvalue,
        s.spearman_max_abs_rho,
        int(s.passes_spearman),
        int(s.stable_in_long_window),
        s.mean_momentum_z,
    )


def _set_from_row(r: sqlite3.Row) -> UncorrelatedSet:
    """Rebuild an :class:`UncorrelatedSet` from an ``uncorrelated_sets`` row."""
    return UncorrelatedSet(
        symbols=tuple(json.loads(r["symbols_json"])),
        size=int(r["size"]),
        max_abs_rho=_f(r["max_abs_rho"]),
        mean_abs_rho=_f(r["mean_abs_rho"]),
        max_abs_rho_ci_upper=_f(r["max_abs_rho_ci_upper"]),
        min_eigenvalue=_f(r["min_eigenvalue"]),
        spearman_max_abs_rho=_f(r["spearman_max_abs_rho"]),
        passes_spearman=bool(r["passes_spearman"]),
        stable_in_long_window=bool(r["stable_in_long_window"]),
        mean_momentum_z=_f(r["mean_momentum_z"]),
    )


def _params_from_json(text: str) -> AnalysisParams:
    """Rebuild :class:`AnalysisParams` from JSON, restoring tuple fields."""
    d: dict[str, Any] = json.loads(text)
    d["windows"] = tuple(int(w) for w in d["windows"])
    w = d["momentum_weights"]
    d["momentum_weights"] = (float(w[0]), float(w[1]), float(w[2]))
    return AnalysisParams(**d)


def _quality_from_json(text: str) -> DataQualityReport:
    """Rebuild :class:`DataQualityReport` from JSON."""
    d: dict[str, Any] = json.loads(text)
    return DataQualityReport(
        requested=tuple(d["requested"]),
        analysed=tuple(d["analysed"]),
        per_symbol=tuple(SymbolQuality(**q) for q in d["per_symbol"]),
        primary_source_failures=int(d["primary_source_failures"]),
        fallback_calls=int(d["fallback_calls"]),
    )


# ---------------------------------------------------------------------------
# RunStore
# ---------------------------------------------------------------------------


class RunStore:
    """Read and write analysis runs and job bookkeeping.

    Parameters
    ----------
    conn : sqlite3.Connection
        Connection on a migrated database (see :func:`store.db.migrate`).
    clock : Clock, optional
        Source of ``job_runs`` timestamps; defaults to the system clock.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()

    # -- analysis -----------------------------------------------------------

    def save_analysis(self, result: AnalysisResult) -> None:
        """Persist a complete analysis run in one transaction.

        Parameters
        ----------
        result : AnalysisResult
            Run to store. ``sets_by_size`` keys must be at least 3 (size 2 is ``pairs``).

        Raises
        ------
        StoreError
            If the run id already exists, a ``sets_by_size`` key is invalid, or SQLite fails.
        """
        bad_sizes = [k for k in result.sets_by_size if k < 3]
        if bad_sizes:
            msg = f"sets_by_size keys must be >= 3, got {bad_sizes}"
            raise StoreError(msg)
        a = result.as_of
        run_row = (
            result.run_id,
            a.day.isoformat(),
            a.mode,
            _iso(a.now_utc),
            int(a.provisional),
            a.calendar_source,
            a.note,
            _iso(result.created_at),
            json.dumps(dataclasses.asdict(result.params)),
            json.dumps(list(result.universe)),
            int(result.largest_set_exact),
            json.dumps(dataclasses.asdict(result.quality)),
        )
        corr_rows = [
            (
                result.run_id,
                c.window,
                c.method,
                json.dumps(list(c.symbols)),
                json.dumps(np.asarray(c.values, dtype=float).tolist()),
                c.n_obs,
                c.start_day.isoformat(),
                c.end_day.isoformat(),
            )
            for c in result.correlations
        ]
        set_rows = [
            _set_row(result.run_id, s, i, is_largest=False) for i, s in enumerate(result.pairs)
        ]
        for size in sorted(result.sets_by_size):
            set_rows.extend(
                _set_row(result.run_id, s, i, is_largest=False)
                for i, s in enumerate(result.sets_by_size[size])
            )
        if result.largest_set is not None:
            set_rows.append(_set_row(result.run_id, result.largest_set, 0, is_largest=True))
        mom_rows = [
            (
                result.run_id,
                m.symbol,
                m.trailing_return,
                m.skip_return,
                m.vol_adjusted_return,
                m.realized_vol,
                m.composite_z,
                m.rank,
                m.direction,
            )
            for m in result.momentum
        ]
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO analysis_runs (run_id, as_of_day, as_of_mode, as_of_now,"
                    " as_of_provisional, as_of_calendar_source, as_of_note, created_at,"
                    " params_json, universe_json, largest_set_exact, quality_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    run_row,
                )
                self.conn.executemany(
                    "INSERT INTO correlations (run_id, window, method, symbols_json, values_json,"
                    " n_obs, start_day, end_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    corr_rows,
                )
                self.conn.executemany(
                    f"INSERT INTO uncorrelated_sets ({_SET_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    set_rows,
                )
                self.conn.executemany(
                    "INSERT INTO momentum_scores (run_id, symbol, trailing_return, skip_return,"
                    " vol_adjusted_return, realized_vol, composite_z, rank, direction)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    mom_rows,
                )
        except sqlite3.Error as exc:
            msg = f"saving analysis {result.run_id} failed: {exc}"
            raise StoreError(msg) from exc

    def load_analysis(self, run_id: str) -> AnalysisResult:
        """Load a run saved by :meth:`save_analysis`.

        Parameters
        ----------
        run_id : str
            Run identifier.

        Returns
        -------
        AnalysisResult
            The reconstructed run; correlations and momentum keep their saved order.

        Raises
        ------
        StoreError
            If the run does not exist or SQLite fails.
        """
        try:
            run = self.conn.execute(
                "SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                msg = f"analysis run {run_id!r} not found"
                raise StoreError(msg)
            corr = self.conn.execute(
                "SELECT * FROM correlations WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
            sets = self.conn.execute(
                "SELECT * FROM uncorrelated_sets WHERE run_id = ? ORDER BY size, rank",
                (run_id,),
            ).fetchall()
            mom = self.conn.execute(
                "SELECT * FROM momentum_scores WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        except sqlite3.Error as exc:
            msg = f"loading analysis {run_id} failed: {exc}"
            raise StoreError(msg) from exc

        correlations = tuple(
            CorrelationMatrix(
                window=int(r["window"]),
                method=cast("Any", r["method"]),
                symbols=tuple(json.loads(r["symbols_json"])),
                values=np.asarray(json.loads(r["values_json"]), dtype=float),
                n_obs=int(r["n_obs"]),
                start_day=date.fromisoformat(r["start_day"]),
                end_day=date.fromisoformat(r["end_day"]),
            )
            for r in corr
        )
        pairs: list[UncorrelatedSet] = []
        by_size: dict[int, list[UncorrelatedSet]] = {}
        largest: UncorrelatedSet | None = None
        for r in sets:
            s = _set_from_row(r)
            if r["is_largest"]:
                largest = s
            elif s.size == 2:
                pairs.append(s)
            else:
                by_size.setdefault(s.size, []).append(s)
        momentum = tuple(
            MomentumScore(
                symbol=r["symbol"],
                trailing_return=_f(r["trailing_return"]),
                skip_return=_f(r["skip_return"]),
                vol_adjusted_return=_f(r["vol_adjusted_return"]),
                realized_vol=_f(r["realized_vol"]),
                composite_z=_f(r["composite_z"]),
                rank=int(r["rank"]),
                direction=cast("Direction", r["direction"]),
            )
            for r in mom
        )
        return AnalysisResult(
            run_id=run["run_id"],
            as_of=AsOf(
                day=date.fromisoformat(run["as_of_day"]),
                mode=cast("AsOfMode", run["as_of_mode"]),
                now_utc=_dt(run["as_of_now"]),
                provisional=bool(run["as_of_provisional"]),
                calendar_source=cast("Any", run["as_of_calendar_source"]),
                note=run["as_of_note"],
            ),
            created_at=_dt(run["created_at"]),
            params=_params_from_json(run["params_json"]),
            universe=tuple(json.loads(run["universe_json"])),
            correlations=correlations,
            pairs=tuple(pairs),
            sets_by_size={k: tuple(v) for k, v in sorted(by_size.items())},
            largest_set=largest,
            largest_set_exact=bool(run["largest_set_exact"]),
            momentum=momentum,
            quality=_quality_from_json(run["quality_json"]),
        )

    def latest_run_id(self) -> str | None:
        """Return the id of the latest run by as-of day, or None if there are none.

        Runs are ordered by ``as_of_day``, then ``created_at``, then insertion order, so a
        historical ``--as-of`` run created later never becomes "latest".

        Returns
        -------
        str | None
            Run id with the greatest ``(as_of_day, created_at)``.
        """
        row = self._one(
            "SELECT run_id FROM analysis_runs"
            " ORDER BY as_of_day DESC, created_at DESC, rowid DESC LIMIT 1",
            (),
        )
        return None if row is None else str(row["run_id"])

    def previous_run_id(self, run_id: str) -> str | None:
        """Return the run created immediately before ``run_id``.

        Runs are ordered by ``created_at`` and then by insertion order (creation order, unlike
        :meth:`latest_run_id`, which ranks by as-of day first).

        Parameters
        ----------
        run_id : str
            Reference run.

        Returns
        -------
        str | None
            Id of the preceding run, or None if ``run_id`` is the first run or unknown.
        """
        row = self._one(
            "SELECT p.run_id FROM analysis_runs AS p JOIN analysis_runs AS c ON c.run_id = ?"
            " WHERE p.created_at < c.created_at"
            " OR (p.created_at = c.created_at AND p.rowid < c.rowid)"
            " ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1",
            (run_id,),
        )
        return None if row is None else str(row["run_id"])

    # -- portfolio ----------------------------------------------------------

    def save_portfolio(self, run_id: str, ctx: PortfolioContext) -> None:
        """Persist the account snapshot and positions for ``run_id`` (replacing any previous).

        Parameters
        ----------
        run_id : str
            Existing analysis run.
        ctx : PortfolioContext
            Account (or the error that prevented reading it) and positions.

        Raises
        ------
        StoreError
            If the run does not exist or SQLite fails.
        """
        acct = ctx.account
        if acct is None:
            acct_row: tuple[object, ...] = (run_id, None, None, None, None, None, None, None, None)
        else:
            acct_row = (
                run_id,
                _iso(acct.taken_at),
                acct.cash,
                acct.equity,
                acct.buying_power,
                acct.portfolio_value,
                acct.last_equity,
                acct.currency,
                int(acct.paper),
            )
        pos_rows = [
            (
                run_id,
                p.symbol,
                p.qty,
                p.avg_entry_price,
                p.current_price,
                p.market_value,
                p.cost_basis,
                p.unrealized_pl,
                p.unrealized_plpc,
                p.change_today,
            )
            for p in ctx.positions
        ]
        try:
            with self.conn:
                self.conn.execute("DELETE FROM position_snapshots WHERE run_id = ?", (run_id,))
                self.conn.execute(
                    "INSERT OR REPLACE INTO account_snapshots (run_id, taken_at, cash, equity,"
                    " buying_power, portfolio_value, last_equity, currency, paper, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*acct_row, ctx.error),
                )
                self.conn.executemany(
                    "INSERT INTO position_snapshots (run_id, symbol, qty, avg_entry_price,"
                    " current_price, market_value, cost_basis, unrealized_pl, unrealized_plpc,"
                    " change_today) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    pos_rows,
                )
        except sqlite3.Error as exc:
            msg = f"saving portfolio for {run_id} failed: {exc}"
            raise StoreError(msg) from exc

    def load_portfolio(self, run_id: str) -> PortfolioContext:
        """Load the portfolio context saved for ``run_id``.

        Parameters
        ----------
        run_id : str
            Analysis run.

        Returns
        -------
        PortfolioContext
            Account (None if it was not read), positions in saved order, and error.

        Raises
        ------
        StoreError
            If nothing was saved for ``run_id`` or SQLite fails.
        """
        try:
            a = self.conn.execute(
                "SELECT * FROM account_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
            rows = self.conn.execute(
                "SELECT * FROM position_snapshots WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        except sqlite3.Error as exc:
            msg = f"loading portfolio for {run_id} failed: {exc}"
            raise StoreError(msg) from exc
        if a is None:
            msg = f"no portfolio snapshot for run {run_id!r}"
            raise StoreError(msg)
        account = None
        if a["taken_at"] is not None:
            account = AccountSnapshot(
                taken_at=_dt(a["taken_at"]),
                cash=_f(a["cash"]),
                equity=_f(a["equity"]),
                buying_power=_f(a["buying_power"]),
                portfolio_value=_f(a["portfolio_value"]),
                last_equity=_f(a["last_equity"]),
                currency=a["currency"],
                paper=bool(a["paper"]),
            )
        positions = tuple(
            PositionSnapshot(
                symbol=r["symbol"],
                qty=_f(r["qty"]),
                avg_entry_price=_f(r["avg_entry_price"]),
                current_price=_f(r["current_price"]),
                market_value=_f(r["market_value"]),
                cost_basis=_f(r["cost_basis"]),
                unrealized_pl=_f(r["unrealized_pl"]),
                unrealized_plpc=_f(r["unrealized_plpc"]),
                change_today=_f(r["change_today"]),
            )
            for r in rows
        )
        return PortfolioContext(account=account, positions=positions, error=a["error"])

    # -- email --------------------------------------------------------------

    def log_email(self, run_id: str, receipt: SendReceipt, recipients: Sequence[str]) -> None:
        """Record a successful send for ``run_id``.

        Parameters
        ----------
        run_id : str
            Analysis run the email reported.
        receipt : SendReceipt
            Backend confirmation.
        recipients : Sequence[str]
            Addresses the message went to (stored as a JSON list).
        """
        self._write(
            "INSERT INTO email_log (run_id, backend, message_id, recipients, sent_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                receipt.backend,
                receipt.message_id,
                json.dumps(list(recipients)),
                _iso(receipt.sent_at),
            ),
        )

    def email_sent_for(self, run_id: str) -> bool:
        """Return whether an email has been logged for ``run_id``.

        Parameters
        ----------
        run_id : str
            Analysis run.

        Returns
        -------
        bool
            True if at least one ``email_log`` row exists for the run.
        """
        row = self._one("SELECT 1 FROM email_log WHERE run_id = ? LIMIT 1", (run_id,))
        return row is not None

    def email_count(self, run_id: str) -> int:
        """Return the number of ``email_log`` rows for ``run_id``.

        Parameters
        ----------
        run_id : str
            Analysis run.

        Returns
        -------
        int
            Number of logged sends (0 if none).
        """
        row = self._one("SELECT COUNT(*) AS n FROM email_log WHERE run_id = ?", (run_id,))
        return 0 if row is None else int(row["n"])

    # -- jobs ---------------------------------------------------------------

    def job_started(self, job: str, run_date: date, stale_after: timedelta | None = None) -> bool:
        """Claim ``(job, run_date)`` with status ``running``.

        A new row is inserted when none exists. An existing row is reclaimed (reset to
        ``running`` with ``started_at`` = now) when its status is ``retry``, or when it is
        ``running`` with ``started_at`` at least ``stale_after`` ago (a crash mid-job). Rows
        with any other status (``ok``, ``error``, ``missed``) are final.

        Parameters
        ----------
        job : str
            Job name.
        run_date : date
            Exchange-local date the job fires for.
        stale_after : timedelta or None
            Age after which a ``running`` row is considered abandoned; None never reclaims
            ``running`` rows.

        Returns
        -------
        bool
            True if this call inserted or reclaimed the row; False if another claim holds.
        """
        now = self._clock.now()
        key = (job, run_date.isoformat())
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO job_runs (job, run_date, status, started_at)"
                    " VALUES (?, ?, 'running', ?)",
                    (*key, _iso(now)),
                )
                if cur.rowcount == 1:
                    return True
                row = self.conn.execute(
                    "SELECT status, started_at FROM job_runs WHERE job = ? AND run_date = ?", key
                ).fetchone()
                if row is None or not _reclaimable(row, now, stale_after):
                    return False
                cur = self.conn.execute(
                    "UPDATE job_runs SET status = 'running', started_at = ?, finished_at = NULL,"
                    " detail = NULL WHERE job = ? AND run_date = ? AND status = ?"
                    " AND started_at = ?",
                    (_iso(now), *key, row["status"], row["started_at"]),
                )
        except sqlite3.Error as exc:
            msg = f"job_started({job}, {run_date}) failed: {exc}"
            raise StoreError(msg) from exc
        return cur.rowcount == 1

    def job_status(self, job: str, run_date: date) -> tuple[str, datetime] | None:
        """Return the status and ``started_at`` of ``(job, run_date)``.

        Parameters
        ----------
        job : str
            Job name.
        run_date : date
            Exchange-local date the job fires for.

        Returns
        -------
        tuple[str, datetime] or None
            ``(status, started_at)``, or None when no row exists.
        """
        row = self._one(
            "SELECT status, started_at FROM job_runs WHERE job = ? AND run_date = ?",
            (job, run_date.isoformat()),
        )
        return None if row is None else (str(row["status"]), _dt(str(row["started_at"])))

    def job_finished(self, job: str, run_date: date, status: str, detail: str) -> None:
        """Record the outcome of ``(job, run_date)``.

        If the job was never started, a row is created with ``started_at`` equal to now.

        Parameters
        ----------
        job : str
            Job name.
        run_date : date
            Exchange-local date the job fired for.
        status : str
            Status: ``"ok"``, ``"error"`` or ``"missed"`` (final) or ``"retry"`` (reclaimable).
        detail : str
            Free-text detail.
        """
        now = _iso(self._clock.now())
        self._write(
            "INSERT INTO job_runs (job, run_date, status, started_at, finished_at, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job, run_date) DO UPDATE SET status = excluded.status,"
            " finished_at = excluded.finished_at, detail = excluded.detail",
            (job, run_date.isoformat(), status, now, now, detail),
        )

    # -- api usage ----------------------------------------------------------

    def api_usage_today(self, provider: str, day: date) -> int:
        """Return the number of API credits used by ``provider`` on ``day``.

        Parameters
        ----------
        provider : str
            Provider name.
        day : date
            Usage day.

        Returns
        -------
        int
            Recorded count (0 if none).
        """
        row = self._one(
            "SELECT count FROM api_usage WHERE provider = ? AND day = ?",
            (provider, day.isoformat()),
        )
        return 0 if row is None else int(row["count"])

    def add_api_usage(self, provider: str, day: date, n: int) -> None:
        """Add ``n`` to the usage count of ``provider`` on ``day``.

        Parameters
        ----------
        provider : str
            Provider name.
        day : date
            Usage day.
        n : int
            Credits to add.
        """
        self._write(
            "INSERT INTO api_usage (provider, day, count) VALUES (?, ?, ?)"
            " ON CONFLICT(provider, day) DO UPDATE SET count = count + excluded.count",
            (provider, day.isoformat(), n),
        )

    # -- internals ----------------------------------------------------------

    def _one(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        """Execute a read query and return its first row, wrapping SQLite errors."""
        try:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            msg = f"query failed: {exc}"
            raise StoreError(msg) from exc
        return row

    def _write(self, sql: str, params: tuple[object, ...]) -> None:
        """Execute one write statement in a transaction, wrapping SQLite errors."""
        try:
            with self.conn:
                self.conn.execute(sql, params)
        except sqlite3.Error as exc:
            msg = f"write failed: {exc}"
            raise StoreError(msg) from exc
