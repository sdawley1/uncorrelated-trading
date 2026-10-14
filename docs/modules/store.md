# store

SQLite persistence (stdlib `sqlite3`) for bars, the trading calendar cache, analysis runs and
job bookkeeping. Database file: `$UT_DATA_DIR/uncorrelated_trading.sqlite3`.

## Modules

- `store/db.py`: `connect(path)` opens the file (creating parent directories) with
  `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000` and
  `row_factory=sqlite3.Row`. `migrate(conn)` creates every table and index from `SCHEMA` and
  records `SCHEMA_VERSION` (1); it is idempotent.
- `store/bars.py`: `BarStore` upserts bars (`INSERT OR REPLACE` on `(symbol, day)`), returns the
  last stored day per symbol, loads inclusive date ranges sorted by `(symbol, day)`, counts
  available days, and caches calendar sessions (`calendar_days`) plus the last refresh instant
  (`meta` key `calendar_last_refreshed`).
- `store/runs.py`: `RunStore` saves and loads `AnalysisResult` (one transaction), portfolio
  snapshots, the email log, job runs and API usage counters.

## Storage conventions

- Dates are `YYYY-MM-DD`; instants are ISO 8601 UTC strings with microseconds. Naive datetimes
  are rejected with `ValueError`.
- Correlation matrices are JSON nested lists; `AnalysisParams` and `DataQualityReport` are JSON
  objects (`dataclasses.asdict`); tuples come back as tuples.
- `uncorrelated_sets`: pairs are `size=2, is_largest=0`; `sets_by_size[k]` entries are
  `size=k, is_largest=0`; the largest set is `is_largest=1, rank=0`. `rank` is the index within
  the saved tuple, so order is preserved. Empty `sets_by_size` entries are not stored.
- SQLite stores NaN as NULL; NULL floats load as `nan`.
- `job_started(job, run_date)` is an `INSERT OR IGNORE` and returns False when the row already
  exists, which makes scheduled jobs fire at most once per local date (no double send).
  Timestamps come from the injected `Clock`.
- Every `sqlite3.Error` is re-raised as `StoreError`.
