# Final whole-branch review: uncorrelated-trading (main, e426905..86d1459)

Reviewer: Senior Code Reviewer (read-only). Scope: all of `src/uncorrelated_trading`, `config/default.toml`,
`pyproject.toml`, both workflows, README and docs, plus targeted tests. Controller-verified gates (ruff, format,
mypy --strict, 409 tests, 93.75 % coverage) were not re-run. I ran: `trader --help`, the offline dry run,
`python3.11 -m compileall` + `ast.parse(feature_version=(3,11))` on all sources (both clean), and four small
scripts in the scratch directory to confirm behaviours suspected from reading (results quoted inline).

## Strengths

- As-of resolution (`calendar.py`) is clean and correct for all five modes; sessions come from calendar rows so
  early closes need no special cases; all wall-time arithmetic goes through `zoneinfo` (`datetime.combine(day, t, tz)`),
  so DST is handled in both `resolve_as_of` and `scheduler.daily_at` (there is a DST test). `data_request_end` enforces
  the SIP guard as specified and the alpaca error mapper explains the SIP message if it ever appears.
- The Alpaca SIP/IEX adapter, Twelve Data adapter and composite are well separated; credentials are only ever
  sent in headers, every `__repr__` masks secrets, `Settings.__repr__` masks, a redaction filter is installed on the
  package logger and its handlers, `.env*` is git-ignored, fixtures contain no key-like strings. I found no path by
  which a key reaches logs, workbook, email or git.
- Global constraints hold: no order code anywhere (grep for `submit_order|OrderRequest|OrderSide|...` is empty);
  dependencies exactly the declared list; `alpaca`/`gspread` imported lazily only inside the four permitted modules;
  no `os.path`, no naive `now()`, no threads/multiprocessing; sources compile under Python 3.11.16.
- Uncorrelated-set search is correct: candidate-list DFS enumerates each combination once, the bounded heap keeps
  the `cap` best by `max|rho|`, Bron-Kerbosch with Tomita pivoting on the complement graph is implemented correctly,
  the shared deadline is sticky so later phases degrade gracefully, and the greedy incumbent guarantees a largest set
  is always reported. Fisher CI (`tanh(atanh|r| + 1.96/sqrt(n-3))`), `eigvalsh` minimum, Spearman flag and
  long-window stability match the spec exactly. Momentum math matches A8 line by line.
- Idempotency design is sound: `job_runs(job, run_date)` claim before run, `email_log` check before send,
  `--dry-run` never records a send, Resend carries an `Idempotency-Key`, restart tests exist and pass.
- Error taxonomy maps cleanly onto exit codes; README exit-code table, command table, env var table and config keys
  all match the code (I cross-checked `docs/configuration.md` and `docs/operations.md` against `cli.py`/`config.py`).
- The dry run produces a sensible brief and a 10-sheet workbook; the text brief is well laid out and includes the
  statistical and dividend caveats the spec asked for.

## Issues

### Critical (Must Fix)

**C1. A run whose as-of day has no bars is silently analysed on older data and labelled with the as-of day, with
every symbol reported "ok".**
`src/uncorrelated_trading/analysis/returns.py:98-104` takes the last `n_days+1` days of the *union of observed days
<= end_day* and never checks that the window actually ends on `end_day`; `analysis/pipeline.py:289-296` only checks
there are enough days; `analysis/pipeline.py:450-451` then rewrites every fetch-time `dropped_missing` record of an
analysed symbol to `status="ok"` (the 97f2c4b fix). Confirmed with the CSV fixture (last bar 2026-10-13), as-of
2026-10-14 and ten `dropped_missing` records in: result `as_of.day=2026-10-14`, `analysed=10`, correlation
`end_day=2026-10-13`, all statuses `ok`, detail "no bar for 2026-10-14". The brief would say "Data through
2026-10-14 ... Dropped: none".
Why it matters: this is the exact path the scheduler takes when the 16:25 refresh was missed and the 07:00 `send`
falls back to `daily_job` in `pre_open` mode on a laptop whose network is not yet up (Alpaca connection errors ->
3 retries -> empty -> every symbol `dropped_missing`). The user receives a brief claiming yesterday's close is
included when it is not. The post-close "fewer than half present" fallback (`jobs.py:236`) shows the design intent
is that the as-of label reflects the data; this hole bypasses it for `pre_open`, `intraday`, `non_trading_day` and
for `trader analyze --as-of D` whenever D's bars are absent. It also contradicts Review Focus 2 ("never forward-filled")
in spirit: the window end is silently substituted.
Fix: (a) in `run_analysis`, after building the primary window, require `built.prices.index[-1] == end_day` and raise
`AnalysisError(f"no bars for as-of day {end_day}; last available day is {last}")` otherwise (`analyze_job` then exits 4
and the scheduler logs an error instead of mailing a mislabelled brief); (b) optionally generalise the `fetch_job`
fallback at `jobs.py:236` to all modes with a neutral note ("bars for D unavailable; using previous session"), so an
outage still yields an honest run on the previous session. Add a unit test for both.

### Important (Should Fix)

**I1. The scheduler never retries a job that fails with a retryable error, so one transient failure at 07:00 costs
the whole day's email.** `scheduler.py:180-189` marks the `(job, date)` row `error` on any exception and `tick` never
revisits it (`scheduler.py:208-212` skips existing rows). `EmailSendError(retryable=True)` (SMTP 4xx, timeouts,
network down), `DataSourceError(retryable=True)` and transient `StoreError` are all treated as final. On a desktop
that wakes at 07:00 the network is routinely unavailable for the first seconds, which is precisely when the
within-grace catch-up fires. The spec's "at most once per local date" was written to prevent double sends, not to
forbid retrying a send that never happened. This is also the right place to resolve the deferred "stale `running`
row" item. Fix: in `_run`, when the exception is a `retryable=True` application error, write status `retry`
(or delete the claim) and leave `self._due[job.name]` at the same fire instant plus a backoff (e.g. 5 min), so the
next tick retries while `now - due <= misfire_grace`; let `job_started` reclaim rows whose status is `retry`, or
`running` with `started_at` older than the grace. Keep `ok`/non-retryable `error` as final. Add a test: SMTP raises
retryable once, succeeds on the next tick, exactly one `email_log` row.

**I2. `--as-of <today>` during the session is accepted as `post_close` and stores partial-day bars as closes;
the future check uses the UTC date.** `calendar.py:479-493`: `snap_as_of` compares `requested > now.date()` in UTC
and returns `mode="post_close", provisional=False` for any calendar trading day. Confirmed: at 11:00 ET on 2026-10-14,
`snap_as_of(2026-10-14)` -> `post_close`, request end `14:44 ET` the same day (Alpaca then returns the in-progress
daily bar, which `fetch_job` upserts as that day's close); at 21:00 ET on 2026-10-14, `--as-of 2026-10-15` is
accepted (UTC date is already the 15th) and resolves to `post_close 2026-10-15`. A user testing with
`trader daily --as-of $(date +%F) --dry-run` at lunchtime pollutes the store with intraday prints for all 80 symbols;
the next nightly overlap re-fetch repairs them but flags up to 80 `close_mismatch "restated:"` rows and triggers a
full re-fetch of each. Fix: compute `today` in the exchange zone (pass `tz`), reject `requested > today`, and if
`requested == today` and `now < close + settle_minutes` either refuse with a clear message or resolve through
`resolve_as_of` (which yields `intraday`/`post_close_settling` with `provisional=True`). Document in README
"As-of semantics".

**I3. `CachedCalendar` makes exactly one remote attempt per instance, so under `trader run` the calendar is never
refreshed again; after ~60 days it degrades permanently to the static calendar.** `calendar.py:272,286-299,316`:
`_remote_attempted` is set on the first call and never cleared; the 30-day `max_age_days` and the `today+60d`
cache horizon are therefore only honoured by one-shot commands. Confirmed with a `FakeClock`: day 0 `source=alpaca`,
day 70 and 71 `source=static` with still exactly one remote call. In the long-running mode the spec recommends,
ad hoc closures (e.g. a national day of mourning) stop being seen after two months of uptime and every brief says
`calendar: static`. Fix: record `_last_attempt: datetime | None` and allow a new attempt when
`now - _last_attempt >= timedelta(days=1)` (or `max_age`) and the cache is stale or does not cover the range; the
"do not hammer a failing remote" goal is preserved by the one-day spacing. Test with the fake clock across 70 days.

**I4. Window alignment uses the union of observed days, so a single stray bar from any one symbol drops every other
symbol and aborts the run.** `returns.py:98-104` builds the window from all days seen for any wanted symbol;
`pipeline.py:331-336` then raises when fewer than three survive. Confirmed: adding one `twelvedata` bar for S01 on
2026-10-14 to the fixture -> `AnalysisError: only 0 symbols survive the 60-day primary window`. A fallback bar from
Twelve Data on a day Alpaca has none (different holiday handling for an ADR, a provider glitch) or a stray row in a
user CSV is enough. Fix: define the window days from the trading calendar, or as the days on which at least half of
the wanted symbols have a bar (then drop symbols missing any of those days, and drop/ignore bars on other days with a
`dropped_missing`-style note for the offending symbol). Property test: adding one extra-day bar for one symbol
changes at most that symbol's status.

**I5. The Twelve Data per-run fallback cap and the cross-check sample are enforced per `CompositeDataSource.fetch`
call, and `fetch_job` makes one call per retried symbol, so the cap is bypassed and every retried symbol is
cross-checked.** `jobs.py:254-262` calls `_fetch_symbols(c, [symbol], ...)` for each missing symbol; in
`composite.py:137,166-194` the sample is `crosscheck_sample(wanted=[symbol], 0.2, day)` -> always that one symbol
(1 credit if the primary returned it), and `composite.py:240` applies `fallback_max` to a one-element batch. The
README promise "At most 20 fallback symbols are fetched per run" (`README.md:301`) is therefore false; the worst
case is bounded by the universe size (~80 extra credits plus ~7 s of primary back-off per retried symbol, about 16
minutes on a first-run Alpaca outage), and the 750/day budget still protects the account, so this is not a budget
blow-out, but it is a spec/doc violation and avoidable waste. Fix: give `fetch_job` a per-run `remaining_fallback`
counter passed into `fetch(...)` (or a `fallback_max=0, crosscheck=False` flag for retry calls), and let the
restatement re-fetch skip cross-checks too (ledger line 70).

### Minor (Nice to Have)

- **M1.** `RunStore.latest_run_id` (`store/runs.py:342-353`) orders by `created_at`, and both `send --latest` and the
  scheduler's `send` job (`scheduler.py:317`) use it. Running `trader analyze --as-of 2026-06-01` in the evening makes
  that historical run "latest", and the 07:00 email reports June. Prefer ordering by `(as_of_day, created_at)` or
  skipping runs whose `as_of_day` is older than the previous trading day in the scheduler.
- **M2.** With `EMAIL_BACKEND=resend`, `--force` does not resend: the `Idempotency-Key` is fixed per run and
  recipients (`jobs.py:366-382`, `resend_sender.py:289-292`), and Resend replays the original response for 24 h.
  README line 252 says `--force` resends. Suffix the key with an attempt counter when `force=True`, or document it.
- **M3.** The redaction filter (`logging_setup.py:41-52`) rewrites `msg` and `args` but not `exc_info`; `_JsonFormatter`
  and the text formatter emit tracebacks unredacted. No current exception text carries a secret, but a future
  `requests` URL-with-key or SDK message would. Redact the formatted exception in a custom `Formatter.formatException`.
- **M4.** `America/New_York` is hard-coded in `data/composite.py:31,162,335`, `data/csv_source.py:231` and
  `report/summary.py:39` while everything else honours `UT_TIMEZONE`. Harmless for NYSE; pass `tz` through for
  consistency.
- **M5.** `DataQualityReport.primary_source_failures` and `fallback_calls` are both set to the count of
  `fallback_used` rows (`pipeline.py:463-470`); the composite's real `primary_failures`/`fallback_calls` counters are
  discarded in `jobs._fetch_symbols` (`jobs.py:180-181`). The "N primary-source failures; N fallback calls" line is
  therefore always two equal numbers.
- **M6.** `.github/workflows/ci.yml:158-163`: `uv sync --python ${{ matrix.python }}` is followed by plain `uv run`
  steps. With `.python-version = 3.12` present, `uv run` may recreate `.venv` for 3.12, so the 3.11 matrix leg can
  silently test 3.12. Not verifiable here (no remote); set `env: UV_PYTHON: ${{ matrix.python }}` on the job (or add
  `--python` to each step). Sources do compile under 3.11 (checked locally).
- **M7.** Provisional bars fetched in `post_close_settling` are stored like any other; the next day's overlap compare
  (`jobs.py:134-157`) may flag them `close_mismatch "restated:"` and trigger a full re-fetch. Expected and bounded,
  but noisy; consider excluding the previous run's provisional day from the restatement check or raising the
  tolerance for it.
- **M8.** Under the scheduler the Google Sheet is published twice a day (refresh and send both call `report_job`,
  `scheduler.py:314,319`, `jobs.py:442-447`). Harmless (quota is ample); pass `publish_sheets=False` from `refresh`.
- **M9.** `FetchOutcome.bars_written` counts the restatement re-fetch rows a second time (`jobs.py:191,196`). Cosmetic.
- **M10.** No `LICENSE` file although README and `pyproject.toml` say MIT (ledger line 75). One-minute fix before
  publishing the repository.

## Deferred-minor triage (ledger extract)

- Line 65, websockets.legacy DeprecationWarning filter: **fine to leave** -- already done in `pyproject.toml`
  (`filterwarnings` ignores it) so nothing remains.
- Line 70, test reads private `_fallback`; restatement re-fetch triggers a second cross-check: **fine to leave** --
  the extra credit is budget-gated and bounded; fold the cross-check skip into the I5 fix if that is done.
- Line 73, crash mid-job leaves a `running` row and the date is never retried: **not a must-fix on its own, but must
  be covered by I1** -- a crash during `refresh` self-heals next morning via `send -> daily_job`, whereas a crash or
  transient failure during `send` loses that day's email; the retry/reclaim mechanism proposed in I1 addresses both.
- Line 75, no LICENSE file and OWNER placeholders in CHANGELOG links: **fine to leave for handoff** -- README is
  honest about it and the placeholders cannot be resolved until the remote exists; add `LICENSE` before `gh repo create`.

## Declined to judge

- External API facts (Alpaca SIP 15-minute rule, in-progress daily bars intraday, Twelve Data credits, Resend
  idempotency window): no credentials here; judged against the spec's A2 statements and provider documentation.
- Google Sheets publisher against the real API: no service account; reviewed for exception wrapping only.
- Statistical design (Pearson tau 0.20, 60-day window, z-score weights, winsorisation, 12-1 analogue): fixed by the
  brainstorm decisions in A1/A8.
- Plaintext secrets in `.env` and `ALPACA_PAPER=false` live-account reads: spec decisions (A5).
- Repeating the same previous-session brief on exchange holidays (scheduler weekdays mode and cron Option 2):
  documented behaviour in `docs/operations.md`.
- `min_history_fraction` accepted but ignored; `close_mismatch` reused for restatements; universe trimmed to 80;
  sub-agent `Co-Authored-By` lines: controller rulings in the ledger.
- Windows service and launchd specifics in `docs/operations.md`: not testable on this host.
- CI not having run remotely (no `gh`): controller ruling; I compiled under 3.11 locally instead.
- Performance of `set_diagnostics` over all C(80,2)=3160 pairs each run (one 2x2 `eigvalsh` each): milliseconds, fine.
- Empty `From:`/`To:` headers in a `--dry-run` without email settings: by design for the file outbox.

## Recommendations

1. Fix C1 first (two-line guard in `run_analysis` plus a test); it is the only finding that can make the delivered
   numbers lie about their own date.
2. Then I1 and I3, which only matter for the recommended `trader run` mode but will bite within the first weeks
   (sleeping laptop) or months (60-day calendar horizon) of unattended use.
3. I2 and I4 are robustness against inputs a user will plausibly supply (`--as-of today`, a stray CSV row or
   fallback bar); both are small, localised changes with obvious tests.
4. I5 and the Minor items can follow in a second pass; update `README.md:301` if I5 is not done.
5. Add `LICENSE` and replace `OWNER` in `CHANGELOG.md` when the GitHub remote is created.

## Assessment

**Ready to hand off? With fixes.** The build is solid, well tested and faithful to the spec in every area the Review
Focus named, but C1 lets the bot mail a brief labelled with a trading day whose data it does not have, on a code path
the scheduler takes after a missed refresh; that must be closed before the user relies on it, and I1-I3 should be
fixed before it is left running unattended.

Findings: 1 Critical, 5 Important, 10 Minor.
