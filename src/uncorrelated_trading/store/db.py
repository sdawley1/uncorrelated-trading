"""SQLite connection setup and schema migration.

All dates are stored as ``YYYY-MM-DD`` strings and all instants as ISO 8601 UTC strings;
no sqlite type detection is used.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from uncorrelated_trading.errors import StoreError

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bars (symbol TEXT NOT NULL, day TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL NOT NULL, volume INTEGER, source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (symbol, day));
CREATE INDEX IF NOT EXISTS ix_bars_day ON bars(day);
CREATE TABLE IF NOT EXISTS calendar_days (day TEXT PRIMARY KEY, open_utc TEXT NOT NULL, close_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_runs (run_id TEXT PRIMARY KEY, as_of_day TEXT NOT NULL, as_of_mode TEXT NOT NULL, as_of_now TEXT NOT NULL, as_of_provisional INTEGER NOT NULL, as_of_calendar_source TEXT NOT NULL, as_of_note TEXT NOT NULL, created_at TEXT NOT NULL, params_json TEXT NOT NULL, universe_json TEXT NOT NULL, largest_set_exact INTEGER NOT NULL, quality_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_runs_created ON analysis_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS correlations (run_id TEXT NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE, window INTEGER NOT NULL, method TEXT NOT NULL, symbols_json TEXT NOT NULL, values_json TEXT NOT NULL, n_obs INTEGER NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL, PRIMARY KEY (run_id, window, method));
CREATE TABLE IF NOT EXISTS uncorrelated_sets (run_id TEXT NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE, size INTEGER NOT NULL, rank INTEGER NOT NULL, is_largest INTEGER NOT NULL DEFAULT 0, symbols_json TEXT NOT NULL, max_abs_rho REAL, mean_abs_rho REAL, max_abs_rho_ci_upper REAL, min_eigenvalue REAL, spearman_max_abs_rho REAL, passes_spearman INTEGER, stable_in_long_window INTEGER, mean_momentum_z REAL, PRIMARY KEY (run_id, size, rank, is_largest));
CREATE TABLE IF NOT EXISTS momentum_scores (run_id TEXT NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE, symbol TEXT NOT NULL, trailing_return REAL, skip_return REAL, vol_adjusted_return REAL, realized_vol REAL, composite_z REAL, rank INTEGER, direction TEXT, PRIMARY KEY (run_id, symbol));
CREATE TABLE IF NOT EXISTS account_snapshots (run_id TEXT PRIMARY KEY REFERENCES analysis_runs(run_id) ON DELETE CASCADE, taken_at TEXT, cash REAL, equity REAL, buying_power REAL, portfolio_value REAL, last_equity REAL, currency TEXT, paper INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS position_snapshots (run_id TEXT NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE, symbol TEXT NOT NULL, qty REAL, avg_entry_price REAL, current_price REAL, market_value REAL, cost_basis REAL, unrealized_pl REAL, unrealized_plpc REAL, change_today REAL, PRIMARY KEY (run_id, symbol));
CREATE TABLE IF NOT EXISTS email_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, backend TEXT NOT NULL, message_id TEXT, recipients TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_runs (job TEXT NOT NULL, run_date TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, detail TEXT, PRIMARY KEY (job, run_date));
CREATE TABLE IF NOT EXISTS api_usage (provider TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (provider, day));
"""  # noqa: E501

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database at ``path``.

    Parameters
    ----------
    path : Path
        Database file. Missing parent directories are created.

    Returns
    -------
    sqlite3.Connection
        Connection with ``journal_mode=WAL``, ``synchronous=NORMAL``, ``foreign_keys=ON``,
        ``busy_timeout=5000`` and ``row_factory=sqlite3.Row``; no type detection.

    Raises
    ------
    StoreError
        If the directory cannot be created or SQLite fails to open the file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), detect_types=0)
    except (OSError, sqlite3.Error) as exc:
        msg = f"cannot open database {path}: {exc}"
        raise StoreError(msg) from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as exc:
        conn.close()
        msg = f"cannot configure database {path}: {exc}"
        raise StoreError(msg) from exc
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes and record :data:`SCHEMA_VERSION` (idempotent).

    Parameters
    ----------
    conn : sqlite3.Connection
        Open connection.

    Raises
    ------
    StoreError
        On any SQLite failure.
    """
    try:
        conn.executescript(SCHEMA)
        with conn:
            row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
            if row[0] == 0:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    except sqlite3.Error as exc:
        msg = f"schema migration failed: {exc}"
        raise StoreError(msg) from exc
