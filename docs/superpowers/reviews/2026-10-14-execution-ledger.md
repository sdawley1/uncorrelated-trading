# SDD ledger — plan: /home/sd2402/.claude/plans/i-would-like-to-enumerated-lemur.md

Spec: Part A of the same file (reachable). Repo: /scratch/gpfs/TORQUATO/sd2402/uncorrelated-trading, branch main (fresh project; user-approved plan says work on main).

## Pre-flight scan (2026-10-14)

| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 models <-> T3 calendar | AsOf(day, mode, now_utc, provisional, calendar_source, note) vs resolve_as_of tests | consistent after edit |
| T1 models <-> T4 store | UncorrelatedSet 10 fields vs uncorrelated_sets columns; analysis_runs columns vs AsOf | consistent |
| T1 models <-> T6 | UncorrelatedSet fields vs set_diagnostics | consistent |
| T3 ratelimit <-> T9/T10 | TokenBucket.acquire(), DailyBudget.spend/remaining | consistent |
| T3 CachedCalendar <-> T4 BarStore | upsert_trading_days/trading_days/calendar_last_refreshed/set_calendar_refreshed | T3 tests with fake store; wired in T15 |
| T5 <-> T11 | build_price_matrix/log_returns/correlation_matrix signatures | consistent |
| T6/T7 <-> T11 | search_uncorrelated_sets(..., long_pearson, momentum_z), momentum_scores | consistent |
| T9 <-> T10 <-> T15 | MarketDataSource.fetch_daily_bars(symbols, start: date, end_utc: datetime) | A4 updated to match |
| T12 <-> T13 <-> T14 | BriefInputs, holdings_rows, tables() | consistent |
| T2 <-> T15 | Settings.from_env(require_alpaca, require_email) | defined in T2 |
| T15 <-> T16 | jobs.* and RunStore.job_started/job_finished | consistent |
| T1 self | pyproject deps include tzdata; ruff rules DTZ, PTH; conftest uses datetime.UTC (3.11+) | ok |
| T1 self | .gitignore lacks .superpowers/ | add (ruling 5) |
| T17 self | requires gh CLI | gh not installed (ruling 3) |
| Rubric check | no task mandates assertion-free tests or verbatim duplication | ok |

## Rulings
- Ruling: Wave tasks (2-8, 9-14) dispatch in parallel to independent sub-agents despite the skill's one-at-a-time rule — the user explicitly required multi-agent parallel execution and the plan assigns disjoint files per task — cost if wrong: merge conflicts in shared files, mitigated by the conflict rule.
- Ruling: Task reviews are performed by the lead (Fable 5.1) reading the review package directly, not by dispatched reviewer subagents — the user explicitly required lead verification — cost if wrong: lead context growth.
- Ruling: `gh` is not installed; Task 17 writes all CI files and documents the push commands but does not create the GitHub remote; the user is told how to do it — cost if wrong: CI not exercised remotely before handoff.
- Ruling: git repo was initialised by the lead during setup; Task 1 skips `git init`.
- Ruling: `.superpowers/` is added to `.gitignore` in Task 1.
- Ruling: Implementer model = opus (Opus 5.5), effort medium, per user instruction.

## Progress
Task 1: complete (commit 96dc524 amended -> see git log; lead review clean; implementer deviations accepted: /data/ ignore pattern, ruff extend-exclude docs/superpowers, handler-level redaction filter, socket connect guard)
- Ruling: `Clock`, `SystemClock`, `FakeClock` hoisted by the lead into `src/uncorrelated_trading/clock.py` before wave 1 — Tasks 3 and 8 both consume it in parallel — cost if wrong: none beyond an extra import path; `ratelimit.py` imports from `clock.py`.
- Ruling: `make_sender(settings, clock)` moves from Task 8 to Task 15 (`app.build_container`) — it needs `Settings` from Task 2 which runs in parallel — cost if wrong: none.
- Ruling: tests/ has no `__init__.py`; test basenames must be unique across tests/; shared `make_bars` is imported as `from conftest import make_bars`.
- Ruling: coverage `fail_under = 85` stays in pyproject (CI enforces it from now); sub-agents run the suite without `--cov` locally.
Wave 1 dispatched (Tasks 2-8, parallel, opus medium). BASE for wave 1 = 4fd99f4. Agent ids: T2 ae12a5af6efd3a72d, T3 afeb7ce73dd73dbb0, T4 a9cea01a1f940a0c1, T5 a316e5d4d3c1cec32, T6 a99c2b2af202e821c, T7 a0df43b8af1280640, T8 a95d33a5ad956b6de.
- Ruling: sub-agent commits carry `Co-Authored-By: Claude Opus 5.5` (their harness enforces it); accepted as accurate attribution; lead commits use Fable 5.1 — cost if wrong: cosmetic.
Task 7: complete (commit bd46fe2, lead review clean; implementer adjusted the additive-constant property test, extra param validation accepted)
Task 5: complete (commit 65a532a, lead review clean; zero-variance correlation -> 0.0 accepted)
Task 8: complete (commit 3c06106, lead review clean; all SMTP 4xx retryable accepted; make_sender deferred to Task 15)
Task 4: complete (commit c209380, lead review clean)
- Ruling: `RunStore` cannot persist empty `sets_by_size` entries; downstream (Tasks 11-13, 15) treat a missing size key as an empty tuple — cost if wrong: a missing "no sets of size N" line in the report.
Task 2: complete (commit 9d3b5f8, lead review clean)
- Ruling: universe trimmed to 80 symbols by dropping HES, X, GOLD (acquired/renamed in 2025); spec A5 text and docs copy to be synced in Task 17 — cost if wrong: none.
- Ruling: `analysis.min_history_fraction` is validated and dropped (no model field); Task 17 removes it from default.toml and docs — cost if wrong: none (behaviour already equals 1.0).
Task 3: complete (commit 743f375, lead review clean)
- Ruling: `CachedCalendar` keeps serving a stale cache that still covers the range when the remote refresh fails (spec A6 reading), falling back to static only when uncovered — accepted — cost if wrong: a late-announced closure missed until next successful refresh.
Task 6: complete (commit 423b332, lead review clean; largest-set search aborts immediately once the deadline has fired — accepted per brief)
Wave 1 verified: 229 tests pass, ruff/mypy clean, coverage 93.8 %. HEAD 423b332.
- Ruling: lead writes `data/base.py`, `broker/base.py`, `report/inputs.py` (BriefInputs + holdings_rows) and `tests/factories.py` before wave 2 so Tasks 9/10 and 12/13 can run in parallel; Task 14 is dispatched after Task 13 (needs `tables()`) — cost if wrong: small lead-authored surface reviewed only by the final review.
Lead contracts committed: 0ca1cc5 (data/base.py, broker/base.py, report/inputs.py, tests/factories.py).
Wave 2 dispatched (Tasks 9-13, parallel, opus medium). BASE for wave 2 = 0ca1cc5. Agent ids: T9 a744369124e121e6c, T10 a312f48fb133d2080, T11 a95691e2b1af72705, T12 ad9421eea2bf2f42b, T13 a2128366cba38a2e5. Task 14 waits for Task 13.
Task 12: complete (commit 477d2d3, lead review clean; golden brief verified by reading)
Task 11: fix round 1/5 dispatched (1 open — analysed symbols carry dropped_* quality status; base c3f0030)
Task 13: complete (commit 2ae2677, lead review clean; workbook opened and inspected)
Task 14 dispatched (opus medium), BASE 57276bb.
Task 11: fix round 1/5 (1 addressed, 0 open; commits c3f0030..97f2c4b)
Task 11: complete (commits c3f0030..97f2c4b, lead re-review clean)
Task 9: complete (commit 57276bb, lead review clean; integration tests skipped locally, no keys)
Task 10: complete (commit a8b5833, lead review clean; 4 primary attempts with 1/2/4 s sleeps and k-step crosscheck rotation accepted)
Task 14: complete (commit 56160b2, lead review clean). Wave 2 verified: 345 tests, coverage 93.2 %, HEAD 56160b2.
- minor (deferred): DeprecationWarning from alpaca-py's websockets.legacy import in test output; Task 17 adds a filterwarnings ignore for `websockets.legacy`.
- Ruling: overlap restatements are reported with status `close_mismatch` (detail prefixed "restated:") since the SymbolQuality Literal has no `restated` value — cost if wrong: one combined status in the Data Quality sheet.
- Ruling: Task 15 adds CLI globals `--config` and `--data-dir`, an offline TOML fixture (S01..S10 universe), and a read-only `RunStore.previous_run_id` (permitted edit to Task 4 files) — cost if wrong: none.
Task 15 dispatched (opus medium), BASE 56160b2.
Task 15: complete (commit 313f21c, lead review clean; offline dry run executed by lead: exit 0, .eml + xlsx produced; Container.dry_run skips email_log accepted)
- minor (deferred): one test reads private `_fallback`; restatement re-fetch may trigger a second crosscheck for that symbol (budget-gated).
Tasks 16 and 17 dispatched in parallel (opus medium), BASE 313f21c.
Task 16: complete (commit 358b19e, lead review clean; catch-up loop from last due time and claim-at-fire idempotency accepted; brief's DST example corrected by implementer)
- minor (deferred): a process crash mid-job leaves a `running` job_runs row and that date is never retried (at-most-once by design); manual `trader send --latest` recovers. Final review to judge whether a stale-`running` reclaim is needed before merge.
Task 17: complete (commit 86d1459, lead review: gate green 409 tests / 93.75 %, CLI help matches README)
- minor (deferred): no LICENSE file (README and pyproject say MIT); CHANGELOG compare links use OWNER placeholder until a remote exists.
All implementation tasks complete at 86d1459. Final whole-branch review dispatched (model fable) + lead Task 18 verification.
Task 18 (lead verification): fresh clone passes 409 tests on Python 3.12 and 3.11; forbidden-pattern grep clean (no order code, no os.path, no print in src, no naive now(), no real-looking keys outside placeholders); offline dry run exit 0; `calendar --date 2026-11-27` shows 13:00 early close.
Lead findings for the final fix wave:
- Important: default `UT_CONFIG_FILE` is the relative path `config/default.toml`, so `trader` fails with "config file not found" from any other cwd (config.py:358). Fix: ship default.toml inside the package (importlib.resources) and fall back to it when neither UT_CONFIG_FILE/--config nor ./config/default.toml exists.
- Important: `check-config` returns 0 for a `.env` copied verbatim from `.env.example` (keys of X's, `/path/to/service-account.json`). Fix: treat values made only of X/x (len >= 8) or starting with `/path/to/` as placeholders -> ConfigError; when sheets are enabled require `GOOGLE_SERVICE_ACCOUNT_FILE` to exist.
Final review (fable, high): 1 Critical (C1 mislabelled as-of when bars absent), 5 Important (I1 no retry within grace, I2 snap_as_of today/UTC, I3 calendar single remote attempt, I4 union-of-days window, I5 fallback cap per call), 10 Minor. Report: final-review-report.md.
- Ruling: fix C1, I1-I5, M1, M2, M3, M5, M6, M8, M10 plus lead findings L1 (packaged default config) and L2 (placeholder rejection) in ONE fix wave; skip M4 (hard-coded NY tz in 3 helpers, harmless for NYSE), M7 (provisional-bar restatement noise, bounded), M9 (bars_written double count, cosmetic) — cost if wrong: cosmetic inconsistencies only.
Fix wave dispatched (opus medium), FIX_BASE 86d1459. Brief: fix-wave-brief.md.
Fix wave: 10 commits f0e42ce..f583c0d; all 14 findings addressed per fixer; follow-up requested: stale_running_after=15 min independent of grace (I1 concern 3).
Fix wave follow-up commit f829ba4 (stale_running_after=15 min). Gate at f829ba4: 459 tests, 94.3 %. Scoped re-review dispatched (opus high) on 86d1459..f829ba4.
Scoped re-review (86d1459..f829ba4): all 14 findings ADDRESSED except I1-followup (restart <15 min after crash skips the day's fire; reproduced). New minor: retry warning log arg bypasses redaction. Out-of-scope: .env.example UT_CONFIG_FILE overrides packaged fallback; I4 majority rule can drop a day on which most symbols lack bars (non-blocking).
- Ruling: one further targeted fix (I1-followup + two minors) is dispatched instead of parking, because the gap defeats the user's core requirement of an unattended daily email on the documented restart-on-failure deployments; the lead verifies by reproduction — cost if wrong: a little extra review time.
- Ruling: I4 majority-day rule accepted as is (a day most symbols lack is excluded; kept symbols get a two-day return); a calendar-based window is noted as a possible follow-up — cost if wrong: slight return distortion on partial-outage days.
Fix wave follow-up 2: c406a7f; re-review gap closed (lead reproduced: one send at ~07:15 after 07:01 restart). Gate at c406a7f: 463 tests, 94.4 %. DONE.
