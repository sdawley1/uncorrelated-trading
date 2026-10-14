# Final fix wave report

Status: DONE_WITH_CONCERNS (all findings fixed; one verification expectation differs because of the I2 fix; see "Concerns").

Commits (on `main`, after 86d1459):

| SHA | Subject |
|---|---|
| f0e42ce | fix(analysis): never label a run with an as-of day that has no bars (C1) |
| 7527660 | fix(scheduler): retry retryable job failures within the misfire grace (I1) |
| 14d51ac | fix(calendar): resolve --as-of against the exchange-local today (I2) |
| 36d248b | fix(calendar): let a long-running CachedCalendar refresh again (I3) |
| 6f49788 | fix(analysis): build price windows from majority days, not the union (I4) |
| e07fe66 | fix(data): enforce the Twelve Data fallback cap per run (I5) |
| 680f80b | feat(config): ship the default config inside the package (L1) |
| 0880154 | feat(config): reject placeholder values copied from .env.example (L2) |
| 2f8b8c6 | fix: address minor review findings (M1, M2, M3, M5, M6, M8, M10) |
| f583c0d | docs: document the final fix wave |

All commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Files were staged by path only.

## Per finding

TDD: for every item the new tests were written first and run red (failure output matched the defect, e.g. I5 `assert 3 == 1` on fallback calls, I1 `TypeError: job_started() takes 3 positional arguments`, C1 "DID NOT RAISE"), then green after the fix.

- **C1**
  - (a) `analysis/pipeline.py`: after each window's `build_price_matrix`, raise `AnalysisError("no bars for as-of day D; last available day is X")` when `prices.index[-1] != as_of.day`.
  - (b) `jobs.py`: the "fewer than half have a bar" fallback now applies in every mode. The new public `fallback_note(as_of)` gives "bars for D unavailable; using previous session", with " (not yet published)" inserted after "unavailable" in post-close modes so the spec wording is kept. The `FALLBACK_NOTE` constant was removed.
  - Tests: `test_pipeline.py::test_as_of_day_without_bars_raises` and `test_jobs.py::test_fetch_falls_back_in_any_mode[intraday|pre_open|non_trading_day]`. This replaces `test_fetch_never_falls_back_before_the_close`, an intentional behaviour change.
- **I1**
  - `RunStore.job_started(job, run_date, stale_after=None)` reclaims rows with status `retry`, and `running` rows whose `started_at` is older than `stale_after`. It uses an optimistic UPDATE keyed on the old status and `started_at`.
  - `Scheduler(..., retry_backoff_seconds=300)` and the new `is_retryable(exc)`: `DataSourceError`/`EmailSendError` use their `.retryable`; `StoreError` and `OSError` are always retryable, because `StoreError` has no flag and `errors.py` is frozen.
  - A retryable failure is recorded as `retry` when `now + backoff - due <= grace`. The due instant is kept and the job is skipped until `now >= retry_at`. Otherwise the failure is recorded as a final `error`. The scheduler passes `job.misfire_grace` as `stale_after`.
  - Tests: `test_scheduler.py::test_retryable_send_failure_is_retried_after_backoff` (SMTP 421 once, then success, exactly one `email_log` row), `test_other_retryable_errors_are_retried[DataSourceError|StoreError|OSError]`, `test_non_retryable_error_is_final`, `test_retries_stop_at_the_grace`; `test_store_runs.py::test_job_started_reclaims_retry_rows` and `test_job_started_reclaims_stale_running_row` (crash simulation).
- **I2**
  - `snap_as_of(requested, calendar, now_utc, tz="America/New_York", settle_minutes=20)`: today is computed in `tz` and later dates raise `ValueError`.
  - `requested == today` delegates to `resolve_as_of`. The note is "--as-of override D is today; <resolve note>".
  - `jobs.resolve_run_as_of` passes `settings.timezone` and `post_close_settle_minutes`.
  - README "As-of semantics" is updated.
  - Tests: `test_calendar.py::test_snap_as_of_future_uses_exchange_date` and `test_snap_as_of_today_resolves_like_now[pre_open|intraday|post_close_settling|post_close]`.
- **I3**
  - `CachedCalendar._last_attempt: datetime | None` replaces `_remote_attempted`.
  - A remote attempt is allowed when at least one day has passed since the last attempt and the cache is stale or does not cover the range.
  - Tests: `test_cached_calendar_long_running_refreshes_again` covers 70 fake days: the remote is called at days 0, 31 and 62 and `source` stays "alpaca". `test_cached_calendar_failed_remote_retried_after_a_day` checks the remote is not called again within 12 h, is called after 24 h, and `source` returns to "alpaca".
- **I4**
  - `analysis/returns.py`: window days are the days on which at least half of the wanted symbols that have any bars (up to `end_day`) have a bar. This uses the new public `window_days()`; `pipeline._available_returns` uses the same rule.
  - Tests: property test `test_returns_props.py::test_extra_day_bar_for_one_symbol_changes_nothing_else` (a stray bar on any non-trading day from 10 days before the history to END changes nothing for the other symbols), `test_returns.py::test_stray_bar_on_extra_day_does_not_drop_other_symbols` and `test_pipeline.py::test_stray_extra_day_bar_does_not_abort_run`.
- **I5**
  - `CompositeDataSource.fetch(..., *, fallback_budget=None, crosscheck=True)`: the cap for a call is `min(fallback_max, fallback_budget)`. The drop detail now reads "fallback cap N per run reached".
  - `jobs.fetch_job` keeps a `_RunCounters` object across all of a run's `fetch` calls. Per-symbol retries and the restatement re-fetch pass `crosscheck=False`. The README "at most 20 fallback symbols per run" line is now true and extended.
  - Tests: `test_composite.py::test_fallback_budget_caps_below_fallback_max`, `test_crosscheck_can_be_disabled_per_call`; `test_jobs.py::test_fallback_cap_and_crosscheck_are_per_run`.
- **L1**
  - `src/uncorrelated_trading/config_default.toml` is a byte copy of `config/default.toml`.
  - New `config.packaged_config_path()` and `config.default_config_path()`. Without `UT_CONFIG_FILE`/`--config`, `./config/default.toml` is used when it exists, else the packaged file.
  - `check-config` prints "(packaged default)" after the path when the packaged file is used.
  - `uv build --wheel` was checked: the wheel contains `uncorrelated_trading/config_default.toml`.
  - Tests: `test_config.py::test_packaged_default_config_is_identical_to_repo_copy`, `test_config_falls_back_to_packaged_default`, `test_local_config_preferred_over_packaged`; `test_cli.py::test_check_config_outside_repo_uses_packaged_config`.
- **L2**
  - `config.is_placeholder()` rejects a run of 8 or more `X` or 8 or more `x`, optionally after a 1-2 letter prefix with an optional `_`, and any value starting with `/path/to/`. The prefix is needed because `.env.example` uses `PKXXXX...` and `re_XXXX...`, which are not "entirely X", yet the brief's test requires ALPACA_API_KEY to be rejected.
  - Keys checked: the Alpaca keys when `require_alpaca`, `SMTP_PASSWORD`/`RESEND_API_KEY` when `require_email`, and always `TWELVEDATA_API_KEY`, `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_FILE`.
  - With Sheets enabled and the file not a placeholder, a missing file gives "GOOGLE_SERVICE_ACCOUNT_FILE does not exist: ...".
  - Tests: `test_env_example_copied_verbatim_is_rejected` (names ALPACA_API_KEY, ALPACA_SECRET_KEY, TWELVEDATA_API_KEY, SMTP_PASSWORD, GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE), `test_placeholder_values_rejected[...]`, `test_non_placeholder_values_accepted[...]`, `test_missing_service_account_file_rejected`.
  - Adjusted test: `test_sheets_enabled_needs_both_vars` now creates the key file, an intentional change.
- **M1**: `latest_run_id` orders by `as_of_day DESC, created_at DESC, rowid DESC`. `previous_run_id` keeps creation order, and its docstring was updated. Test: `test_store_runs.py::test_latest_run_id_prefers_latest_as_of_day`.
- **M2**
  - New `RunStore.email_count(run_id)`.
  - `send_job(force=True)`, when not a dry run and N > 0 sends are logged, uses the key `"<key>-f<N>"`. With N = 0 the key is left unchanged: there is nothing to replay, and the first forced send then keeps the normal key.
  - README documents that `--force` resends.
  - Tests: an extension of `test_jobs.py::test_send_job_refuses_duplicate` (keys "run-key", "run-key-f1", "run-key-f2"), and `test_store_runs.py::test_email_count`.
- **M3**: new `_RedactingFormatter.formatException` applies the shared filter's redaction. `_JsonFormatter` subclasses it and the text format uses it. Test: `test_logging_setup.py::test_redaction_covers_formatted_exceptions` (text and JSON).
- **M5**
  - `run_analysis(..., primary_source_failures=None, fallback_calls=None)`, which falls back to row counting when None.
  - `FetchOutcome.primary_failures`/`fallback_calls` default to 0 and are summed over the run.
  - `analyze_job(..., *, primary_source_failures=None, fallback_calls=None)`. `daily_job` and the scheduler's `refresh` pass the counters; `trader analyze` on stored data keeps the counting.
  - Tests: `test_pipeline.py::test_explicit_composite_counters_override_row_counting`, `test_jobs.py::test_analyze_job_records_composite_counters`, `test_fetch_outcome_carries_composite_counters`.
- **M6**: `.github/workflows/ci.yml` sets job-level `env: UV_PYTHON: ${{ matrix.python }}`. Guard test: `test_config.py::test_ci_uses_matrix_python_and_license_present`. CI cannot be run here (no remote).
- **M8**: `report_job(..., publish_sheets=True)`. The scheduler's `refresh` passes False. Tests: `test_jobs.py::test_report_job_can_skip_sheets`. The `test_scheduler.py::calls` fixture lambdas now accept the new keyword arguments, and `test_refresh_action_fetches_analyses_and_reports` expects `report:r1:no-sheets`; this is an intentional change.
- **M10**: `LICENSE` (MIT, "Copyright (c) 2026 The uncorrelated-trading authors"). README "License" now points to it. Covered by the same guard test as M6.
- **M4, M7, M9**: skipped as instructed.

Docs: README (As-of semantics, `--force`, scheduler retries/latest/Sheets, packaged config, placeholders, fallback cap, exit code 4, License), `docs/configuration.md`, `docs/operations.md` and the CHANGELOG `[Unreleased]` section were all updated.

## Verification (final)

- Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -m "not integration" -q --cov --cov-fail-under=85` gives all checks passed, 101 files formatted, mypy clean (43 files), **457 passed, 3 deselected, coverage 94.37 %**. This is up from 409 tests.
- Offline dry run `--as-of 2026-10-13`: exit 0, 10 of 10 symbols analysed.
- Offline dry run `--as-of 2026-10-14`: exit 0, analysed as of 2026-10-13. See Concerns for the note text.
- C1 fallback path, shown with a FakeClock at 17:30 ET on 2026-10-14 through `app.build_container` and the offline CSV:
  - `fetch_job` falls back to 2026-10-13 with the note "...; bars for 2026-10-14 unavailable (not yet published); using previous session" and analyses 10 symbols ending 2026-10-13.
  - `analyze_job` called directly with as-of 2026-10-14 raises `AnalysisError: no bars for as-of day 2026-10-14; last available day is 2026-10-13`.
  - Script: `scratchpad/c1_demo.py`.
- `cd /tmp && uv run --project <repo> trader check-config` now fails on missing env vars (ALPACA_*, EMAIL_*), exit 2, and no longer says "config file not found". With fake env vars set it exits 0 and prints the `config_default.toml` path followed by "(packaged default)".

## Concerns

1. **`--as-of 2026-10-14` does not show the neutral fetch note when run before the session.** The dry run ran at 02:17 ET on 2026-10-14, which is today in the exchange zone. Because of I2, `--as-of <today>` now resolves through `resolve_as_of` to `pre_open`, so it uses 2026-10-13 directly. The note is "--as-of override 2026-10-14 is today; before today's open; using previous session 2026-10-13", not the fetch fallback note. Run after 16:20 ET it would resolve `post_close` 2026-10-14, then fall back to 2026-10-13 with the "bars for 2026-10-14 unavailable (not yet published); using previous session" note (shown above with a fake clock). Either way the result is 2026-10-13 and exit 0.
2. The L2 placeholder rule is slightly broader than "entirely X", as explained above. It would reject a real key consisting of 1-2 letters followed only by eight or more X, which is practically impossible.
3. Under the brief's I1 rule (`stale_after` = grace), a crashed `running` row becomes reclaimable only once it is older than the grace. By then the same fire is also outside its grace, so in practice the reclaim turns the row into `missed` rather than re-running it. It no longer stays `running` for ever. Same-day re-runs after a crash would need a shorter `stale_after`.
4. M5: `primary_failures` sums over all composite calls of a run, so a symbol that fails in the main fetch and again in its retry counts twice. This is documented on `FetchOutcome`.
5. M2: the `-f<N>` suffix is added only when N > 0.

## Follow-up: I1 crash reclaim (coordinator request)

Commit: see `fix(scheduler): reclaim crashed running claims after 15 minutes` (below the docs commit).

- `Scheduler(..., stale_running_after: timedelta = timedelta(minutes=15))`, also exposed as `DEFAULT_STALE_RUNNING_AFTER`. It does not depend on the grace.
- Both `job_started` calls in `_handle_due` now pass it as `stale_after` (previously `job.misfire_grace`).
- Effect: a claim left `running` by a crash is reclaimed on the first tick more than 15 minutes after it was claimed. If the fire is still within its grace, the job runs again; otherwise the date is recorded as `missed`. This resolves concern 3 above.
- Tests in `tests/unit/test_scheduler.py`, both failing before the change and passing after:
  - `test_crashed_running_row_is_reclaimed_within_grace`: a row is left `running` with `started_at` 07:00; the scheduler starts at 07:20 with a 3 h grace and startup catch-up. The job runs on the first tick, the row ends `ok`, there is exactly one `email_log` row, and nothing re-runs an hour later.
  - `test_recent_running_row_is_not_reclaimed`: a claim 10 minutes old is not re-run.
- Docs: the `scheduler.py` module docstring, `docs/operations.md` and the CHANGELOG now say 15 minutes. The README did not mention reclaiming.
- Checks: ruff and mypy are clean. The scheduler, store and scheduler-property tests give 51 passed. The full suite gives 459 passed, 3 deselected.
- Caveat: a job that legitimately runs longer than 15 minutes would be claimed a second time only by another scheduler process sharing the database. Within one process, ticks are sequential, so this cannot happen.

## Follow-up 2: re-review fixes

Commit: `fix(scheduler): wait for a fresh running claim instead of skipping the fire`.

1. **I1 reclaim gap (Important), fixed.**
   - New `RunStore.job_status(job, run_date) -> tuple[str, datetime] | None`.
   - `Scheduler._handle_due` now returns `(invoked, wait_until)`. When the claim fails, the new `_wait_for_claim` checks the row:
     - `ok`, `error` or `missed`: logged as already ran; `due` advances as before.
     - `running` and not yet stale: `due` is kept and `_retry_at = started_at + stale_running_after`. If that instant is beyond `due + grace`, the fire is logged as missed and `due` advances; the row is left untouched, since it may belong to a live run.
   - `_reclaimable` now uses `>=`, so a claim is reclaimable exactly at `started_at + stale_after`.
   - Tests:
     - `test_restart_right_after_crash_waits_then_reclaims`: the reviewer's scenario (claim `running` at 07:00, restart 07:01, a tick every 30 s to 10:00, 3 h grace). The job runs exactly once, between 07:15 and 07:16; there is one `email_log` row, the status is `ok`, and the next due is tomorrow 07:00. It failed before the fix and passes now. This test replaces `test_recent_running_row_is_not_reclaimed`.
     - `test_running_claim_outlasting_the_grace_is_missed`
     - `test_finished_rows_still_skip_and_advance`
     - `test_store_runs.py::test_job_status`
   - The reviewer's `scratchpad/reclaim_gap.py` now prints `calls: [2026-10-14]` and next due 2026-10-15 07:00 ET.
2. **Retry warning redaction, fixed.** The warning now logs `str(exc)`, which goes through the redaction filter.
3. **`.env.example`, fixed.** `UT_CONFIG_FILE` is commented out, with a note that `./config/default.toml` or else the packaged default is used when it is unset. Test: `test_config.py::test_env_example_leaves_config_file_unset`.

Docs: `docs/operations.md` and `CHANGELOG.md` now say a crashed job is re-run on the first tick after its claim is 15 minutes old, while within the grace.

Verification:
- Covering tests (`tests/unit/test_scheduler.py`, `tests/property/test_scheduler_props.py`, `tests/unit/test_store_runs.py`, `tests/unit/test_config.py`): 114 passed.
- Full gate: ruff check and format clean, mypy clean, 463 passed / 3 deselected, coverage 94.35 %.
- Offline dry run `--as-of 2026-10-13`: exit 0.
