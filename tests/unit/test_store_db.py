import sqlite3
from pathlib import Path

import pytest

from uncorrelated_trading.errors import StoreError
from uncorrelated_trading.store.db import SCHEMA_VERSION, connect, migrate

EXPECTED_TABLES = {
    "schema_version",
    "bars",
    "calendar_days",
    "meta",
    "analysis_runs",
    "correlations",
    "uncorrelated_sets",
    "momentum_scores",
    "account_snapshots",
    "position_snapshots",
    "email_log",
    "job_runs",
    "api_usage",
}


def test_connect_creates_parent_dirs_and_sets_pragmas(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "db.sqlite3"
    conn = connect(path)
    try:
        assert path.parent.is_dir()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_migrate_creates_all_tables_and_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "db.sqlite3")
    try:
        migrate(conn)
        migrate(conn)
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert EXPECTED_TABLES <= names
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert [r["version"] for r in rows] == [SCHEMA_VERSION]
        idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_bars_day", "ix_runs_created"} <= idx
    finally:
        conn.close()


def test_migrate_wraps_sqlite_errors(tmp_path: Path):
    conn = connect(tmp_path / "db.sqlite3")
    conn.close()
    with pytest.raises(StoreError):
        migrate(conn)


def test_connect_wraps_sqlite_errors(tmp_path: Path):
    target = tmp_path / "is_a_dir"
    target.mkdir()
    with pytest.raises(StoreError):
        connect(target)
