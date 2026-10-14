# Part B: Implementation Plan

## Global Constraints

- `requires-python = ">=3.11"`; `.python-version` is `3.12`; all code must pass on 3.11 and 3.12 (no `type` statements, no PEP 695 generics).
- Runtime dependencies are exactly: `numpy`, `pandas`, `alpaca-py`, `requests`, `openpyxl`, `python-dotenv`, `tzdata` (zoneinfo data on Windows). Optional extra `sheets`: `gspread`, `google-auth`. Dev group: `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `pandas-stubs`, `types-requests`, `types-openpyxl`. Adding anything else requires lead approval.
- No multiprocessing, no threads except the scheduler main loop; numpy may use its own BLAS threads.
- No order placement code anywhere. No `submit_order`, `OrderRequest`, or similar imports.
- Secrets only via `.env`/environment; never in TOML, code, tests, fixtures or logs. Fixture JSON uses obviously fake keys.
- All timestamps stored as ISO 8601 UTC strings; dates as `YYYY-MM-DD`; all `datetime` objects tz-aware.
- `models.py` is frozen after Task 1; changes go through the lead.
- Every module has a module docstring; every public function has a NumPy-style docstring and full type hints; `ruff` (rules `E,F,I,B,UP,N,D,ANN,DTZ,PTH`, line length 100, `D` ignores `D203,D212`) and `mypy --strict` pass. `DTZ` forbids naive datetimes; `PTH` forbids `os.path`.
- `alpaca`, `gspread` and `google` are imported only inside `data/alpaca_source.py`, `broker/alpaca_account.py`, `calendar.py` (Alpaca calendar) and `report/sheets.py`, lazily, so the core runs without them in tests.
- Logging passes through a redaction filter that replaces any configured secret value with `***`.
- Tests never touch the network unless marked `@pytest.mark.integration`; `conftest.py` sets `socket` guard for non-integration tests.
- Commit after every task with conventional-commit messages ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Work on branch `main` in this repo (fresh project; no worktree needed). Sub-agents editing in parallel must only touch the files their task lists.

## Review Focus

Spec-implied inputs no single task's tests cover by default; each has a pinned test added to the owning task below.

1. **Run at 16:05 ET on a trading day** (between close and settle): must resolve to `post_close_settling` with today and `provisional=True`, the data request `end` must be at least 16 minutes before now, and if most of today's bars are not yet published the fetch must fall back to the previous day with a note. Pinned in Task 3 (`test_between_close_and_settle_is_provisional_today`, `test_request_end_never_within_sip_guard`) and Task 15 (`test_fetch_falls_back_when_today_bars_missing`).
2. **A symbol delisted or halted mid-window** (bars stop early): must be dropped with `dropped_missing`, never forward-filled, and the run must still succeed with the remaining symbols. Pinned in Task 5 (`test_symbol_with_gap_is_dropped`) and Task 11.
3. **tau so high that every pair qualifies** (e.g. 0.99 with 80 symbols): exact enumeration would explode; the deadline must fire, results must be capped, `largest_set_exact` must be False, and total runtime under 2x the budget. Pinned in Task 6 (`test_deadline_caps_runtime`).
4. **Restart of `trader run` after the morning email already went out**: must not send twice. Pinned in Task 16 (`test_restart_does_not_resend`).
5. **Gmail rejects the SMTP login (535) or Resend returns 429**: must raise `EmailSendError` with `retryable` set correctly, keep the report persisted, and exit with code 5 so a cron retry works. Pinned in Task 8 (`test_smtp_auth_error_not_retryable`, `test_resend_429_retryable`) and Task 15 (`test_send_failure_exit_code_5`).

## Execution protocol (lead + sub-agents)

- **Waves.** Task 1 runs alone. Wave 1 = Tasks 2 to 8 in parallel. Wave 2 = Tasks 9 to 14 in parallel. Wave 3 = Task 15, then Task 16, then Task 17, then Task 18. A wave starts only after the lead has verified every task it depends on.
- **Sub-agent brief.** Each sub-agent receives: the spec (Part A), Global Constraints, Review Focus, its own task text, and the `Interfaces` blocks of the tasks it consumes. Model Opus 5.5, effort medium. It works only in the files its task lists, follows TDD step order, and ends by running `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -m "not integration" -q` and committing.
- **Lead verification (Fable 5.1, high effort) per task.** Read the diff in full. Run the task's test file, then the full suite. Check: interfaces match this plan verbatim; no forbidden dependency or order code; docstrings and types present; tests assert behaviour not implementation; property tests have meaningful strategies; secrets absent; Windows path safety (`pathlib`, no `/tmp`); tz-aware datetimes. Reject with a written list of defects, resend to the same sub-agent via SendMessage, re-verify. Two rejections escalate to the lead fixing it directly.
- **Conflict rule.** `pyproject.toml`, `models.py`, `errors.py`, `conftest.py` are owned by Task 1; later tasks may add fixtures only in their own `tests/<layer>/conftest.py`. `cli.py` and `jobs.py` are owned by Task 15; `README.md` by Task 17 (other tasks write module docs in docstrings and a short `docs/modules/<name>.md`).

---

### Task 1: Scaffold, contracts, test infrastructure, CI skeleton

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `README.md` (stub), `CHANGELOG.md`, `config/default.toml` (content from A5), `.github/workflows/ci.yml`, `.github/workflows/integration.yml`
- Create: `src/uncorrelated_trading/__init__.py`, `models.py` (content from A4), `errors.py`, `logging_setup.py`, empty `__init__.py` in `data/`, `broker/`, `store/`, `analysis/`, `report/`, `report/email/`
- Create: `tests/conftest.py`, `tests/unit/test_models.py`, `tests/unit/test_errors.py`, `tests/unit/test_logging_setup.py`, `tests/fixtures/README.md`
- Create: `docs/superpowers/specs/2026-10-14-uncorrelated-trading-design.md` (Part A verbatim), `docs/superpowers/plans/2026-10-14-uncorrelated-trading.md` (Part B verbatim)

**Interfaces:**
- Produces: everything in A4 (`models.py`), `errors.py` hierarchy, `logging_setup.configure_logging(level: str, json_format: bool = False) -> None`, `logging_setup.add_redaction(secrets: Iterable[str]) -> None` (installs a `logging.Filter` on the package logger that replaces each non-empty secret with `***` in `record.msg` and string `record.args`), pytest fixtures `frozen_now` (returns `datetime(2026, 10, 13, 21, 30, tzinfo=UTC)`), `sample_bars` (3 symbols x 300 trading days deterministic geometric random walk, seed 0, built with numpy, returned as `list[Bar]`), `tmp_data_dir` (a `Path`), `no_network` (autouse for non-integration tests; monkeypatches `socket.socket` to raise), pytest markers `integration`, `slow`.

- [ ] **Step 1: Initialise repo and project**

```bash
cd /scratch/gpfs/TORQUATO/sd2402/uncorrelated-trading
git init -b main
uv init --lib --name uncorrelated-trading --python 3.12
# uv creates src/uncorrelated_trading/__init__.py and py.typed; keep both, then replace pyproject.toml with the block below.
```

Write `pyproject.toml`:

```toml
[project]
name = "uncorrelated-trading"
version = "0.1.0"
description = "Advisory bot: uncorrelated energy/commodity equities with momentum scores, delivered by email."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = [
  "numpy>=1.26",
  "pandas>=2.2",
  "alpaca-py>=0.44",
  "requests>=2.32",
  "openpyxl>=3.1",
  "python-dotenv>=1.0",
  "tzdata>=2024.1",
]

[project.optional-dependencies]
sheets = ["gspread>=6.1", "google-auth>=2.30"]

[project.scripts]
trader = "uncorrelated_trading.cli:main"

[dependency-groups]
dev = [
  "pytest>=8.3", "pytest-cov>=5.0", "hypothesis>=6.112",
  "ruff>=0.6", "mypy>=1.11", "pandas-stubs>=2.2", "types-requests>=2.32", "types-openpyxl>=3.1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/uncorrelated_trading"]

[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "D", "ANN", "DTZ", "PTH"]
ignore = ["D203", "D212", "ANN401"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D", "ANN"]

[tool.ruff.lint.pydocstyle]
convention = "numpy"

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src"]
plugins = []

[[tool.mypy.overrides]]
module = ["alpaca.*", "gspread.*", "google.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = [
  "integration: hits real external APIs; needs credentials",
  "slow: takes more than a few seconds",
]
filterwarnings = ["error::DeprecationWarning:uncorrelated_trading.*"]

[tool.coverage.run]
source = ["uncorrelated_trading"]
branch = true

[tool.coverage.report]
fail_under = 85
show_missing = true
```

Write `.gitignore`:

```
.env
.env.*
!.env.example
data/
.venv/
__pycache__/
*.py[cod]
*.sqlite3
dist/
build/
.coverage
coverage.xml
htmlcov/
.mypy_cache/
.ruff_cache/
.pytest_cache/
.hypothesis/
```

Write `.env.example` with every variable from A5, each preceded by a comment line and holding a placeholder like `ALPACA_API_KEY=PKXXXXXXXXXXXXXXXXXX`.

- [ ] **Step 2: Write `models.py` and `errors.py`**

`models.py` is the A4 block verbatim plus module docstring. `errors.py`:

```python
"""Exception hierarchy for uncorrelated_trading."""
from __future__ import annotations


class UncorrelatedTradingError(Exception):
    """Base class for all application errors."""


class ConfigError(UncorrelatedTradingError):
    """Missing or invalid configuration; carries all problems at once."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Configuration errors:\n  - " + "\n  - ".join(problems))


class DataSourceError(UncorrelatedTradingError):
    """A market data provider failed."""

    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"{provider}: {message}")


class RateLimitError(DataSourceError):
    """Provider rate limit hit (always retryable)."""

    def __init__(self, provider: str, message: str = "rate limit exceeded") -> None:
        super().__init__(provider, message, retryable=True)


class StoreError(UncorrelatedTradingError):
    """SQLite persistence failure."""


class AnalysisError(UncorrelatedTradingError):
    """Analysis could not be completed (e.g. too few symbols)."""


class ReportError(UncorrelatedTradingError):
    """Rendering the brief or workbook failed."""


class EmailSendError(UncorrelatedTradingError):
    """Sending the email failed."""

    def __init__(self, backend: str, message: str, *, retryable: bool) -> None:
        self.backend = backend
        self.retryable = retryable
        super().__init__(f"{backend}: {message}")
```

- [ ] **Step 3: Write failing tests for models, errors, logging**

`tests/unit/test_models.py`:

```python
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from uncorrelated_trading.models import Bar


def test_bar_is_frozen():
    bar = Bar("XOM", date(2026, 1, 2), 1.0, 2.0, 0.5, 1.5, 100, "csv")
    with pytest.raises(FrozenInstanceError):
        bar.close = 3.0  # type: ignore[misc]


def test_sample_bars_fixture_shape(sample_bars):
    symbols = {b.symbol for b in sample_bars}
    assert symbols == {"AAA", "BBB", "CCC"}
    assert len(sample_bars) == 900
    days = sorted({b.day for b in sample_bars})
    assert all(d.weekday() < 5 for d in days)
```

`tests/unit/test_errors.py`:

```python
from uncorrelated_trading.errors import ConfigError, RateLimitError


def test_config_error_lists_all_problems():
    err = ConfigError(["ALPACA_API_KEY missing", "EMAIL_TO missing"])
    assert "ALPACA_API_KEY missing" in str(err) and "EMAIL_TO missing" in str(err)


def test_rate_limit_is_retryable():
    assert RateLimitError("alpaca").retryable is True
```

`tests/unit/test_logging_setup.py`:

```python
import logging

from uncorrelated_trading.logging_setup import configure_logging


def test_configure_logging_sets_level(caplog):
    configure_logging("DEBUG")
    assert logging.getLogger("uncorrelated_trading").level == logging.DEBUG


def test_redaction_filter_masks_secret(capsys):
    configure_logging("INFO")
    add_redaction(["SKSECRET123"])
    logging.getLogger("uncorrelated_trading.x").info("key is %s", "SKSECRET123")
    assert "SKSECRET123" not in capsys.readouterr().err
```

(Import `add_redaction` alongside `configure_logging`.)

- [ ] **Step 4: Run tests, confirm failures** (`uv run pytest tests/unit -q` fails on imports).

- [ ] **Step 5: Implement `logging_setup.py` and `conftest.py`**

```python
# logging_setup.py
"""Logging configuration (human or JSON lines)."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, json_format: bool = False) -> None:
    """Configure the ``uncorrelated_trading`` logger hierarchy.

    Parameters
    ----------
    level : str
        Logging level name, e.g. ``"INFO"``.
    json_format : bool
        Emit one JSON object per line instead of human-readable text.
    """
    logger = logging.getLogger("uncorrelated_trading")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    fmt: logging.Formatter = (
        _JsonFormatter()
        if json_format
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
```

`tests/conftest.py`:

```python
from __future__ import annotations

import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from uncorrelated_trading.models import Bar


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def make_bars(symbols: list[str], n_days: int, seed: int = 0, end: date = date(2026, 10, 13)) -> list[Bar]:
    rng = np.random.default_rng(seed)
    days: list[date] = []
    d = end
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    bars: list[Bar] = []
    for s in symbols:
        rets = rng.normal(0.0003, 0.015, size=n_days)
        closes = 100.0 * np.exp(np.cumsum(rets))
        for day, c in zip(days, closes, strict=True):
            bars.append(Bar(s, day, float(c * 0.995), float(c * 1.01), float(c * 0.99), float(c), 1_000_000, "csv"))
    return bars


@pytest.fixture
def sample_bars() -> list[Bar]:
    return make_bars(["AAA", "BBB", "CCC"], 300)


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("integration"):
        return

    def guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access blocked in non-integration tests")

    monkeypatch.setattr(socket, "create_connection", guard)
```

- [ ] **Step 6: CI workflows**

`.github/workflows/ci.yml`:

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv python install ${{ matrix.python }}
      - run: uv sync --all-extras --python ${{ matrix.python }}
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run pytest -m "not integration" --cov --cov-report=xml
      - uses: actions/upload-artifact@v4
        if: matrix.os == 'ubuntu-latest' && matrix.python == '3.12'
        with: { name: coverage-xml, path: coverage.xml }
```

`.github/workflows/integration.yml`: `on: { schedule: [{cron: "0 2 * * 1-5"}], workflow_dispatch: {} }`, single ubuntu job, same setup, then `uv run pytest -m integration -q` with `env:` mapping `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TWELVEDATA_API_KEY` from `secrets.*`; step `if: ${{ secrets.ALPACA_API_KEY != '' }}` is not allowed in `if`, so instead the test suite skips when env is empty.

- [ ] **Step 7: Run everything** `uv sync --all-extras && uv run ruff check . && uv run ruff format . && uv run mypy src && uv run pytest -q`. Expected: all pass, coverage gate may be below 85 (only scaffolding); set `fail_under` enforcement to activate in Task 17 by adding `--cov-fail-under=85` to CI there, not in `addopts`.

- [ ] **Step 8: Docs copies and commit**

Copy Part A and Part B into `docs/superpowers/...`. `git add -A && git commit -m "chore: scaffold uv project, shared models, test infra, CI"` with the attribution trailer.

---

### Task 2: Settings (.env) and AppConfig (TOML)

**Files:**
- Create: `src/uncorrelated_trading/config.py`, `tests/unit/test_config.py`, `tests/property/test_config_props.py`, `docs/modules/config.md`
- Modify: `config/default.toml` only if validation reveals an error in the committed list.

**Interfaces:**
- Consumes: `errors.ConfigError`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool
    twelvedata_api_key: str | None
    email_backend: Literal["smtp", "resend", "file"]
    email_from: str
    email_to: tuple[str, ...]
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_use_ssl: bool
    resend_api_key: str | None
    google_service_account_file: Path | None
    google_sheet_id: str | None
    data_dir: Path
    config_file: Path
    timezone: str
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, dotenv_path: Path | None = None, *, require_alpaca: bool = True, require_email: bool = True) -> "Settings": ...
    # require_alpaca=False / require_email=False are used by `trader ... --offline --dry-run`; missing keys then become None / "" instead of errors
    @property
    def sheets_enabled(self) -> bool: ...
    @property
    def db_path(self) -> Path: ...   # data_dir / "uncorrelated_trading.sqlite3"
    def __repr__(self) -> str: ...   # masks every *key*/*password* field as "***"

@dataclass(frozen=True, slots=True)
class UniverseConfig: symbols: tuple[str, ...]; excluded_patterns: tuple[str, ...]
@dataclass(frozen=True, slots=True)
class DataConfig: backfill_calendar_days: int; overlap_calendar_days: int; adjustment: str; alpaca_feed: str; alpaca_requests_per_minute: int; alpaca_symbols_per_request: int; twelvedata_requests_per_minute: int; twelvedata_requests_per_day: int; twelvedata_fallback_max_per_run: int; crosscheck_fraction: float; close_mismatch_tolerance: float; post_close_settle_minutes: int
@dataclass(frozen=True, slots=True)
class ScheduleConfig: refresh_time: time; send_time: time; send_on_days: Literal["weekdays","trading_days","every_day"]; poll_seconds: int; send_misfire_grace_hours: int; refresh_misfire_grace_hours: int
@dataclass(frozen=True, slots=True)
class ReportConfig: top_pairs: int; top_sets_per_size: int; top_momentum: int; subject_prefix: str
@dataclass(frozen=True, slots=True)
class AppConfig:
    universe: UniverseConfig; data: DataConfig; analysis: AnalysisParams; schedule: ScheduleConfig; report: ReportConfig
    @classmethod
    def load(cls, path: Path) -> "AppConfig": ...
```

- [ ] **Step 1: Failing tests** (`tests/unit/test_config.py`)

```python
from pathlib import Path

import pytest

from uncorrelated_trading.config import AppConfig, Settings
from uncorrelated_trading.errors import ConfigError

GOOD_ENV = {
    "ALPACA_API_KEY": "PKTEST", "ALPACA_SECRET_KEY": "SKTEST",
    "EMAIL_BACKEND": "smtp", "EMAIL_FROM": "me@example.com", "EMAIL_TO": "a@example.com, b@example.com",
    "SMTP_USERNAME": "me@example.com", "SMTP_PASSWORD": "apppass",
}


def test_settings_from_good_env(tmp_path):
    s = Settings.from_env(env={**GOOD_ENV, "UT_DATA_DIR": str(tmp_path)}, dotenv_path=None)
    assert s.email_to == ("a@example.com", "b@example.com")
    assert s.smtp_host == "smtp.gmail.com" and s.smtp_port == 587 and s.alpaca_paper is True
    assert s.db_path == tmp_path / "uncorrelated_trading.sqlite3"


def test_settings_reports_all_missing_at_once():
    with pytest.raises(ConfigError) as ei:
        Settings.from_env(env={"EMAIL_BACKEND": "smtp"}, dotenv_path=None)
    msg = str(ei.value)
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "EMAIL_FROM", "EMAIL_TO", "SMTP_PASSWORD"):
        assert key in msg


def test_resend_backend_requires_key():
    env = {**GOOD_ENV, "EMAIL_BACKEND": "resend"}
    with pytest.raises(ConfigError, match="RESEND_API_KEY"):
        Settings.from_env(env=env, dotenv_path=None)


def test_repr_masks_secrets():
    s = Settings.from_env(env=GOOD_ENV, dotenv_path=None)
    assert "SKTEST" not in repr(s) and "apppass" not in repr(s)


def test_offline_mode_does_not_require_alpaca_or_email():
    s = Settings.from_env(env={}, dotenv_path=None, require_alpaca=False, require_email=False)
    assert s.alpaca_api_key == "" and s.email_backend == "file"


def test_dotenv_does_not_override_os_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ALPACA_API_KEY=FROMFILE\n")
    s = Settings.from_env(env={**GOOD_ENV, "ALPACA_API_KEY": "FROMOS"}, dotenv_path=p)
    assert s.alpaca_api_key == "FROMOS"


def test_default_toml_loads():
    cfg = AppConfig.load(Path("config/default.toml"))
    assert 40 <= len(cfg.universe.symbols) <= 80
    assert cfg.analysis.primary_window in cfg.analysis.windows
    assert cfg.schedule.send_time.hour == 7


@pytest.mark.parametrize("bad, expect", [
    ('[universe]\nsymbols=["XOM","XOM"]\n', "duplicate"),
    ('[universe]\nsymbols=["USO"]\nexcluded_patterns=["USO"]\n', "excluded"),
    ('[analysis]\ntau=1.5\n', "tau"),
])
def test_invalid_toml_rejected(tmp_path, bad, expect):
    p = tmp_path / "c.toml"
    p.write_text(bad)
    with pytest.raises(ConfigError, match=expect):
        AppConfig.load(p)
```

Property test (`tests/property/test_config_props.py`): hypothesis generates lists of 2 to 200 distinct upper-case tickers; `AppConfig` built from a TOML string with those symbols must round-trip them in order.

- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement** `config.py`: `tomllib` parse; missing sections fall back to the A5 defaults (so a partial TOML is valid); collect problems into a list and raise once. `Settings.from_env`: `dotenv_values(dotenv_path)` merged under `env` (env wins); `dotenv_path` defaults to `Path.cwd()/".env"` when `None` is not passed explicitly (use a sentinel so tests can disable file loading). Parse bools with `{"1","true","yes","on"}`.
- [ ] **Step 4: Run tests, lint, mypy; pass.**
- [ ] **Step 5: Commit** `feat(config): settings from env and TOML app config with validation`.

---

### Task 3: Trading calendar, as-of resolution, rate limiting

**Files:**
- Create: `src/uncorrelated_trading/calendar.py`, `src/uncorrelated_trading/ratelimit.py`, `tests/unit/test_calendar.py`, `tests/unit/test_ratelimit.py`, `tests/property/test_calendar_props.py`, `tests/property/test_ratelimit_props.py`, `docs/modules/calendar.md`

**Interfaces:**
- Consumes: `models.TradingDay`, `models.AsOf`.
- Produces:

```python
class TradingCalendar(Protocol):
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

class StaticCalendar:
    """Weekdays minus rule-computed NYSE holidays; 09:30-16:00 sessions, 13:00 early closes."""
    def __init__(self, tz: str = "America/New_York") -> None: ...
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

class CachedCalendar:
    """Alpaca-backed calendar cached in BarStore; falls back to StaticCalendar."""
    def __init__(self, store: "BarStore", fetch_remote: Callable[[date, date], list[TradingDay]] | None, clock: Clock, tz: str, max_age_days: int = 30) -> None: ...
    source: Literal["alpaca", "static"]      # set after first query
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

def nyse_holidays(year: int) -> frozenset[date]: ...
def nyse_early_closes(year: int) -> frozenset[date]: ...
def resolve_as_of(now_utc: datetime, calendar: TradingCalendar, tz: str, settle_minutes: int, calendar_source: Literal["alpaca", "static"] = "static") -> AsOf: ...
def previous_trading_day(day: date, calendar: TradingCalendar) -> date: ...
def snap_as_of(requested: date, calendar: TradingCalendar, now_utc: datetime) -> AsOf: ...   # for --as-of overrides
def data_request_end(as_of: AsOf, tz: str, now_utc: datetime, sip_guard_minutes: int = 16) -> datetime: ...

class Clock(Protocol):
    def now(self) -> datetime: ...          # tz-aware UTC
    def sleep(self, seconds: float) -> None: ...
class SystemClock: ...
class FakeClock:                             # for tests; advance(seconds)
    def __init__(self, start: datetime) -> None: ...
    def advance(self, seconds: float) -> None: ...

class TokenBucket:
    def __init__(self, rate_per_minute: int, clock: Clock) -> None: ...
    def acquire(self) -> None: ...           # blocks via clock.sleep until a token is free

class DailyBudget:
    def __init__(self, limit: int, used_today: Callable[[], int], record: Callable[[int], None]) -> None: ...
    def remaining(self) -> int: ...
    def spend(self, n: int = 1) -> None: ... # raises RateLimitError("twelvedata") when exceeded
```

Holiday rules: New Year's Day (Jan 1; Sunday -> Monday; a Saturday New Year's is not observed on Friday Dec 31), Martin Luther King Jr. Day (3rd Monday Jan), Presidents' Day (3rd Monday Feb), Good Friday (Easter via the Anonymous Gregorian algorithm minus 2 days), Memorial Day (last Monday May), Juneteenth (Jun 19, observed), Independence Day (Jul 4, observed), Labor Day (1st Monday Sep), Thanksgiving (4th Thursday Nov), Christmas (Dec 25, observed). Early closes 13:00 ET: day after Thanksgiving; Jul 3 and Dec 24 when they are weekdays and not themselves holidays. Expected 2026 holidays: Jan 1, Jan 19, Feb 16, Apr 3, May 25, Jun 19, Jul 3, Sep 7, Nov 26, Dec 25; 2027: Jan 1, Jan 18, Feb 15, Mar 26, May 31, Jun 18, Jul 5, Sep 6, Nov 25, Dec 24. The implementer cites https://www.nyse.com/markets/hours-calendars in the docstring. `CachedCalendar` depends on `BarStore` from Task 4; implement it against the `BarStore` interface block and test with a fake store (dict-backed object with the same three calendar methods).

`TradingDay` for a `post_close_settling` check uses `close_utc` from the calendar row, so early closes work automatically.

- [ ] **Step 1: Failing tests** (`tests/unit/test_calendar.py`, key cases)

```python
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from uncorrelated_trading.calendar import StaticCalendar, data_request_end, resolve_as_of

NY = ZoneInfo("America/New_York")
CAL = StaticCalendar()


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


def test_midday_on_trading_day_is_intraday_previous_day():
    a = resolve_as_of(at(2026, 10, 13, 12, 0), CAL, "America/New_York", 20)
    assert a.mode == "intraday" and a.day == date(2026, 10, 12)


def test_pre_open_uses_previous_day():
    a = resolve_as_of(at(2026, 10, 13, 8, 0), CAL, "America/New_York", 20)
    assert a.mode == "pre_open" and a.day == date(2026, 10, 12)


def test_between_close_and_settle_is_provisional_today():
    a = resolve_as_of(at(2026, 10, 13, 16, 5), CAL, "America/New_York", 20)
    assert a.mode == "post_close_settling" and a.day == date(2026, 10, 13) and a.provisional


def test_after_settle_is_post_close_today():
    a = resolve_as_of(at(2026, 10, 13, 16, 25), CAL, "America/New_York", 20)
    assert a.mode == "post_close" and a.day == date(2026, 10, 13) and not a.provisional


def test_early_close_day_after_thanksgiving():
    # 2026-11-27 closes 13:00 ET
    a = resolve_as_of(at(2026, 11, 27, 13, 30), CAL, "America/New_York", 20)
    assert a.mode == "post_close" and a.day == date(2026, 11, 27)


def test_static_calendar_known_holidays_2026():
    days = {d.day for d in CAL.trading_days(date(2026, 1, 1), date(2026, 12, 31))}
    for holiday in [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                    date(2026, 11, 26), date(2026, 12, 25)]:
        assert holiday not in days
    assert date(2026, 10, 13) in days


def test_saturday_uses_friday():
    a = resolve_as_of(at(2026, 10, 17, 9, 0), CAL, "America/New_York", 20)
    assert a.mode == "non_trading_day" and a.day == date(2026, 10, 16)


def test_holiday_uses_prior_trading_day():
    a = resolve_as_of(at(2026, 11, 26, 9, 0), CAL, "America/New_York", 20)
    assert a.day == date(2026, 11, 25)


def test_request_end_never_within_sip_guard():
    now = at(2026, 10, 13, 16, 25)
    a = resolve_as_of(now, CAL, "America/New_York", 20)
    end = data_request_end(a, "America/New_York", now)
    assert end <= now - __import__("datetime").timedelta(minutes=16)
```

`tests/unit/test_ratelimit.py`: bucket with rate 3 and `FakeClock`; four `acquire()` calls must advance the fake clock by at least 20 s; `DailyBudget(limit=2)` allows two `spend()` then raises `RateLimitError`. Property tests: for random acquire sequences, count of acquisitions in any sliding 60 s window never exceeds rate; `resolve_as_of(...).day <= local date` and is always a trading day.

- [ ] **Step 2: Run, confirm fail.** **Step 3: Implement.** `resolve_as_of` per A6; `data_request_end` returns `min(datetime.combine(as_of.day, time(23, 59, 59), tz), now_utc - 16 min)`. **Step 4: Pass, lint, mypy.** **Step 5: Commit** `feat(calendar): as-of resolution, static NYSE calendar, token bucket and daily budget`.

---

### Task 4: SQLite store

**Files:**
- Create: `src/uncorrelated_trading/store/db.py`, `store/bars.py`, `store/runs.py`, `tests/unit/test_store_db.py`, `tests/unit/test_store_bars.py`, `tests/unit/test_store_runs.py`, `tests/property/test_store_props.py`, `docs/modules/store.md`

**Interfaces:**
- Consumes: all `models`, `errors.StoreError`.
- Produces:

```python
# db.py
SCHEMA_VERSION = 1
def connect(path: Path) -> sqlite3.Connection: ...   # WAL, foreign_keys=ON, row_factory=sqlite3.Row, detect no types (store ISO strings)
def migrate(conn: sqlite3.Connection) -> None: ...   # idempotent; creates tables from SCHEMA below; records schema_version

# bars.py
class BarStore:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def upsert(self, bars: Iterable[Bar]) -> int: ...                 # returns rows written
    def last_day(self, symbol: str) -> date | None: ...
    def load(self, symbols: Sequence[str], start: date, end: date) -> list[Bar]: ...
    def days_available(self, symbol: str, start: date, end: date) -> int: ...
    def upsert_trading_days(self, days: Iterable[TradingDay]) -> None: ...
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...   # cached calendar
    def calendar_last_refreshed(self) -> datetime | None: ...
    def set_calendar_refreshed(self, when: datetime) -> None: ...

# runs.py
class RunStore:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def save_analysis(self, result: AnalysisResult) -> None: ...     # runs + correlations + sets + momentum + quality (one transaction)
    def load_analysis(self, run_id: str) -> AnalysisResult: ...
    def latest_run_id(self) -> str | None: ...
    def save_portfolio(self, run_id: str, ctx: PortfolioContext) -> None: ...
    def load_portfolio(self, run_id: str) -> PortfolioContext: ...
    def log_email(self, run_id: str, receipt: SendReceipt, recipients: Sequence[str]) -> None: ...
    def email_sent_for(self, run_id: str) -> bool: ...
    def job_started(self, job: str, run_date: date) -> bool: ...     # False if a row already exists (idempotency)
    def job_finished(self, job: str, run_date: date, status: str, detail: str) -> None: ...
    def api_usage_today(self, provider: str, day: date) -> int: ...
    def add_api_usage(self, provider: str, day: date, n: int) -> None: ...
```

Schema (in `db.py` as a string):

```sql
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
```

- [ ] **Step 1: Failing tests**: `upsert` of the same bars twice yields the same row count in table; `last_day` None for unknown symbol; `load` respects inclusive range and ascending order; `save_analysis` then `load_analysis` round-trips equal to the original (compare field by field, `np.allclose` on matrices); `job_started` returns True then False for the same (job, date); `email_sent_for` flips after `log_email`. Property test: random bar lists with duplicates -> upsert idempotent and `load` returns exactly the unique (symbol, day) set.
- [ ] **Step 2: Fail.** **Step 3: Implement** with `executemany`, explicit `BEGIN`/`COMMIT` via `with conn:`; wrap `sqlite3.Error` in `StoreError`. **Step 4: Pass.** **Step 5: Commit** `feat(store): sqlite schema, bar store and run store`.

---

### Task 5: Returns and correlation

**Files:**
- Create: `src/uncorrelated_trading/analysis/returns.py`, `analysis/correlation.py`, `tests/unit/test_returns.py`, `tests/unit/test_correlation.py`, `tests/property/test_returns_props.py`, `tests/property/test_correlation_props.py`

**Interfaces:**
- Consumes: `models.Bar`, `models.CorrelationMatrix`, `models.SymbolQuality`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class PriceMatrixResult:
    prices: pd.DataFrame                 # index: date ascending, columns: kept symbols, dtype float64
    dropped: tuple[SymbolQuality, ...]   # status dropped_missing or dropped_short_history

def build_price_matrix(bars: Sequence[Bar], symbols: Sequence[str], end_day: date, n_days: int) -> PriceMatrixResult: ...
def log_returns(prices: pd.DataFrame) -> pd.DataFrame: ...
def correlation_matrix(returns: pd.DataFrame, method: Literal["pearson", "spearman"], window: int) -> CorrelationMatrix: ...
```

Semantics: `build_price_matrix` takes the last `n_days + 1` trading days ending at `end_day` as observed across all symbols (union of days); a symbol is `dropped_missing` if it lacks any of those days, `dropped_short_history` if its first bar is after the window start. `log_returns` yields `n_days` rows.

- [ ] **Step 1: Failing tests** including `test_symbol_with_gap_is_dropped` (remove one middle bar from one symbol in `sample_bars`; that symbol appears in `dropped` with `dropped_missing`; the other two remain), `test_log_returns_of_constant_prices_is_zero`, `test_correlation_unit_diagonal_symmetric`, `test_identical_series_rho_one`, `test_anti_series_rho_minus_one`. Property: scaling prices of one symbol by a positive constant leaves the correlation matrix unchanged (within 1e-9); matrix entries within [-1, 1]; permuting symbols permutes the matrix consistently.
- [ ] **Step 2: Fail. Step 3: Implement. Step 4: Pass. Step 5: Commit** `feat(analysis): price matrix, log returns, correlation matrices`.

---

### Task 6: Uncorrelated set discovery

**Files:**
- Create: `src/uncorrelated_trading/analysis/uncorrelated.py`, `tests/unit/test_uncorrelated.py`, `tests/property/test_uncorrelated_props.py`, `docs/modules/uncorrelated.md`

**Interfaces:**
- Consumes: `models.CorrelationMatrix`, `models.UncorrelatedSet`, `calendar.Clock` (for deadline via `time.monotonic`-like injection: accept `monotonic: Callable[[], float]`).
- Produces:

```python
@dataclass(frozen=True, slots=True)
class SetSearchResult:
    pairs: tuple[UncorrelatedSet, ...]
    sets_by_size: dict[int, tuple[UncorrelatedSet, ...]]
    largest: UncorrelatedSet | None
    largest_exact: bool
    deadline_hit: bool

def build_adjacency(pearson: CorrelationMatrix, tau: float) -> np.ndarray: ...   # bool (n,n), True where abs(rho) >= tau, diagonal False
def find_pairs(pearson: CorrelationMatrix, spearman: CorrelationMatrix, tau: float, tau_spearman: float) -> tuple[UncorrelatedSet, ...]: ...
def enumerate_independent_sets(adjacency: np.ndarray, size: int, cap: int, abs_rho: np.ndarray, deadline: float, monotonic: Callable[[], float]) -> tuple[tuple[int, ...], ...]: ...
def largest_independent_set(adjacency: np.ndarray, deadline: float, monotonic: Callable[[], float]) -> tuple[tuple[int, ...], bool]: ...
def fisher_ci_upper_abs(rho: float, n_obs: int, z: float = 1.96) -> float: ...   # tanh(atanh(|rho|) + z / sqrt(n_obs - 3)), clipped to [0, 1]
def set_diagnostics(indices: Sequence[int], pearson: CorrelationMatrix, spearman: CorrelationMatrix, long_pearson: CorrelationMatrix | None, tau: float, tau_spearman: float, momentum_z: Mapping[str, float] | None = None) -> UncorrelatedSet: ...
def search_uncorrelated_sets(pearson: CorrelationMatrix, spearman: CorrelationMatrix, long_pearson: CorrelationMatrix | None, tau: float, tau_spearman: float, max_size_exact: int, cap_per_size: int, time_budget_s: float, momentum_z: Mapping[str, float] | None = None, monotonic: Callable[[], float] = time.monotonic) -> SetSearchResult: ...
```

`stable_in_long_window` is False when `long_pearson` is None or lacks any member. `mean_momentum_z` is 0.0 when `momentum_z` is None.

Algorithm for `enumerate_independent_sets`: recursive extension; state = current tuple `s`, candidate index array `cands` (indices > max(s) and non-adjacent to all of s, maintained via boolean masks); on reaching `size`, record `(max abs_rho within s, s)`; keep a bounded heap of `cap` best by `max_abs_rho`; check `monotonic() > deadline` every 1000 node visits and abort. `largest_independent_set`: greedy (repeatedly take min-degree vertex, remove neighbours) gives lower bound; then Bron-Kerbosch with pivot on the complement adjacency with the deadline; return `(best, exact)`.

- [ ] **Step 1: Failing tests**: hand-built 5x5 matrix with known independent sets; `test_pairs_sorted_ascending`; `test_subsets_of_reported_sets_are_uncorrelated`; `test_largest_set_on_empty_graph_is_everything` (all rho 0 -> largest is all symbols, exact True); `test_largest_set_on_complete_graph_is_singleton`; `test_deadline_caps_runtime` (80 symbols, tau 0.99 so graph is nearly empty, budget 0.5 s via `FakeClock`-style monotonic that advances per call; assert `deadline_hit`, `largest_exact is False`, result still well-formed); `test_order_invariance` (permute symbols, compare sets as frozensets of names). Property (hypothesis): random symmetric matrices in [-1, 1] with unit diagonal, n in 3..12, tau in (0.05, 0.95): every returned set satisfies pairwise bound; `min_eigenvalue <= 1 <= max`; pairs count equals number of i<j below tau.
- [ ] **Steps 2-5** as usual. Commit `feat(analysis): uncorrelated pair and n-tuple discovery with diagnostics`.

---

### Task 7: Momentum

**Files:**
- Create: `src/uncorrelated_trading/analysis/momentum.py`, `tests/unit/test_momentum.py`, `tests/property/test_momentum_props.py`

**Interfaces:**
- Consumes: `models.MomentumScore`, `models.AnalysisParams`.
- Produces: `def momentum_scores(returns: pd.DataFrame, params: AnalysisParams) -> tuple[MomentumScore, ...]` (sorted by rank ascending), raising `AnalysisError` if `len(returns) < params.momentum_long`. Also `def zscore(x: pd.Series) -> pd.Series` (population std; zeros when std is 0).

- [ ] **Step 1: Failing tests**: `test_monotone_trend_ranks_first` (one symbol with steady positive returns beats flat ones); `test_direction_thresholds`; `test_vol_adjusted_zero_when_sigma_zero`; `test_requires_long_window`; `test_rank_is_dense_and_starts_at_one`. Property: composite z-scores have mean within 1e-9 of 0 when weights sum to 1; adding the same constant to all return columns leaves ranks unchanged; permutation invariance of scores by symbol.
- [ ] **Steps 2-5.** Commit `feat(analysis): composite momentum scores`.

---

### Task 8: Email senders

**Files:**
- Create: `src/uncorrelated_trading/report/email/base.py`, `smtp_sender.py`, `resend_sender.py`, `file_sender.py`, `tests/unit/test_smtp_sender.py`, `tests/unit/test_resend_sender.py`, `tests/unit/test_file_sender.py`, `tests/fixtures/resend/send_ok.json`, `tests/fixtures/resend/send_429.json`

**Interfaces:**
- Consumes: `models.EmailMessage`, `models.Attachment`, `models.SendReceipt`, `errors.EmailSendError`, `calendar.Clock`.
- Produces:

```python
class EmailSender(Protocol):
    name: str
    def send(self, message: EmailMessage) -> SendReceipt: ...

def build_mime(message: EmailMessage) -> email.message.EmailMessage: ...   # shared by smtp and file senders

class SmtpSender:
    name = "smtp"
    def __init__(self, host: str, port: int, username: str | None, password: str | None, use_ssl: bool, clock: Clock, timeout_s: float = 30.0, smtp_factory: Callable[..., smtplib.SMTP] | None = None) -> None: ...
class ResendSender:
    name = "resend"
    def __init__(self, api_key: str, clock: Clock, session: requests.Session | None = None, timeout_s: float = 30.0) -> None: ...
class FileSender:
    name = "file"
    def __init__(self, outbox: Path, clock: Clock) -> None: ...   # writes <outbox>/<idempotency_key>.eml and each attachment
def make_sender(settings: Settings, clock: Clock) -> EmailSender: ...   # factory used by jobs
```

Error mapping: SMTP `535`/`SMTPAuthenticationError` -> `EmailSendError(retryable=False)`; `SMTPServerDisconnected`, `socket.timeout`, 4xx transient codes (421, 450, 451, 452) -> `retryable=True`. Resend: 401/403/422 -> not retryable; 429 and 5xx -> retryable (one internal retry after `clock.sleep(2)` for 5xx/429, then raise).

- [ ] **Step 1: Failing tests**: `test_build_mime_has_text_html_and_attachment` (parse back with `email.message_from_bytes`, check `get_content_type()` of parts and attachment filename); `test_smtp_sends_via_starttls` (fake SMTP class records `starttls`, `login`, `send_message` calls); `test_smtp_auth_error_not_retryable`; `test_smtp_disconnect_retryable`; `test_resend_posts_expected_json` (fake `requests.Session` captures body: `from`, `to` list, `attachments[0].content` is base64 of bytes, header `Idempotency-Key`); `test_resend_429_retryable`; `test_resend_401_not_retryable`; `test_file_sender_writes_eml_and_attachment`.
- [ ] **Steps 2-5.** Commit `feat(report): smtp, resend and file email senders`.

---

### Task 9: Alpaca adapters (data source, account source, calendar fetch)

**Files:**
- Create: `src/uncorrelated_trading/data/base.py`, `data/alpaca_source.py`, `broker/base.py`, `broker/alpaca_account.py`, `broker/null_account.py`, `tests/contract/test_alpaca_source.py`, `tests/contract/test_alpaca_account.py`, `tests/integration/test_alpaca_live.py`, `tests/fixtures/alpaca/bars_page1.json`, `bars_page2.json`, `account.json`, `positions.json`, `calendar.json`, `error_sip_recent.json`, `docs/modules/alpaca.md`

**Interfaces:**
- Consumes: `models.Bar`, `TradingDay`, `AccountSnapshot`, `PositionSnapshot`, `errors.DataSourceError`, `RateLimitError`, `ratelimit.TokenBucket`, `calendar.Clock`.
- Produces:

```python
# data/base.py
class MarketDataSource(Protocol):
    name: str
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...

# data/alpaca_source.py
class AlpacaDataSource:
    name = "alpaca"
    def __init__(self, api_key: str, secret_key: str, feed: str, adjustment: str, symbols_per_request: int, bucket: TokenBucket, tz: str, client: Any | None = None) -> None: ...   # client injected in tests; else lazily builds StockHistoricalDataClient
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...
    @staticmethod
    def parse_bars(raw: Mapping[str, Any], tz: str) -> list[Bar]: ...   # raw = BarSet.data-like dict or JSON page {"bars": {...}}

# broker/base.py
class AccountSource(Protocol):
    paper: bool
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> list[PositionSnapshot]: ...
    def trading_days(self, start: date, end: date) -> list[TradingDay]: ...

# broker/alpaca_account.py
class AlpacaAccountSource:
    def __init__(self, api_key: str, secret_key: str, paper: bool, clock: Clock, tz: str, client: Any | None = None) -> None: ...
    @staticmethod
    def parse_account(raw: Mapping[str, Any], taken_at: datetime, paper: bool) -> AccountSnapshot: ...
    @staticmethod
    def parse_positions(raw: Sequence[Mapping[str, Any]]) -> list[PositionSnapshot]: ...
    @staticmethod
    def parse_calendar(raw: Sequence[Mapping[str, Any]], tz: str) -> list[TradingDay]: ...

# broker/null_account.py
class NullAccountSource:            # offline: paper=True, account() raises DataSourceError("alpaca", "offline", retryable=False)
    def __init__(self, calendar: TradingCalendar) -> None: ...
```

Rules: `fetch_daily_bars` chunks symbols, builds `StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=datetime.combine(start, time(0), tz), end=end_utc, limit=10000, adjustment=Adjustment(adjustment), feed=DataFeed(feed))`, calls `bucket.acquire()` before each request, converts each bar's `timestamp` to the exchange-local date (`timestamp.astimezone(ZoneInfo(tz)).date()`), and maps exceptions: `alpaca.common.exceptions.APIError` with status 429 -> `RateLimitError("alpaca")`; status 401/403 containing "subscription" -> `DataSourceError(retryable=False)` whose message quotes Alpaca's text and says to move `end` earlier; 5xx and connection errors -> `retryable=True`. Fixtures are recorded from the public API shape (fake keys, 3 symbols, 5 days).

- [ ] **Step 1: Failing contract tests**: `test_parse_bars_converts_midnight_et_timestamp_to_date` (timestamp `2026-10-13T04:00:00Z` -> `date(2026,10,13)`; also `2026-03-09T04:00:00Z` the day after a DST change -> `date(2026,3,9)`); `test_fetch_chunks_symbols` (3 symbols with `symbols_per_request=2` produce exactly 2 client calls whose symbol lists are `["AAA","BBB"]` and `["CCC"]`); `test_fetch_follows_next_page_token` (fake client returns `bars_page1.json` with a `next_page_token`, then `bars_page2.json`; all bars from both pages are returned); `test_fetch_acquires_bucket_per_request`; `test_sip_recent_error_mapped_not_retryable`; `test_429_mapped_rate_limit`; `test_parse_account_casts_strings`; `test_parse_positions`; `test_parse_calendar_builds_utc_sessions` (open `09:30` and close `16:00` local -> UTC); `test_null_account_raises_offline`. Integration (`tests/integration/test_alpaca_live.py`, marker `integration`, skip if `ALPACA_API_KEY` missing): fetch bars for `["XOM","CVX","XLE"]` over 10 calendar days ending `now - 1 day`, assert at least 5 bars per symbol; `account()` returns `paper is True` when `ALPACA_PAPER` unset.
- [ ] **Steps 2-5.** Commit `feat(alpaca): daily bar source and account/calendar adapter`.

---

### Task 10: Twelve Data source, CSV source, composite with fallback and cross-check

**Files:**
- Create: `src/uncorrelated_trading/data/twelvedata_source.py`, `data/csv_source.py`, `data/composite.py`, `tests/contract/test_twelvedata_source.py`, `tests/unit/test_csv_source.py`, `tests/unit/test_composite.py`, `tests/integration/test_twelvedata_live.py`, `tests/fixtures/twelvedata/time_series_ok.json`, `time_series_429.json`, `time_series_bad_symbol.json`, `tests/fixtures/csv/bars_small.csv` (10 symbols x 300 days from `make_bars`, columns `symbol,day,open,high,low,close,volume`)

**Interfaces:**
- Consumes: `data/base.MarketDataSource` (Task 9 block), `ratelimit.TokenBucket`, `ratelimit.DailyBudget`, `models.SymbolQuality`.
- Produces:

```python
class TwelveDataSource:
    name = "twelvedata"
    def __init__(self, api_key: str, bucket: TokenBucket, budget: DailyBudget, session: requests.Session | None = None, timeout_s: float = 30.0) -> None: ...
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...   # one GET per symbol, interval=1day, adjust=splits (verify; else omit), outputsize=5000
    def latest_closes(self, symbols: Sequence[str], n: int = 3) -> dict[str, list[Bar]]: ...
    @staticmethod
    def parse_time_series(symbol: str, payload: Mapping[str, Any]) -> list[Bar]: ...

class CsvDataSource:
    name = "csv"
    def __init__(self, path: Path) -> None: ...
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...

@dataclass(frozen=True, slots=True)
class CompositeFetchResult:
    bars: list[Bar]
    quality: tuple[SymbolQuality, ...]       # fallback_used / close_mismatch / dropped_missing entries
    primary_failures: int
    fallback_calls: int

class CompositeDataSource:
    name = "composite"
    def __init__(self, primary: MarketDataSource, fallback: MarketDataSource | None, tolerance: float, fallback_max: int, crosscheck_fraction: float, min_budget_for_crosscheck: int, budget: DailyBudget | None, retries: int = 3, backoff_s: Sequence[float] = (1.0, 2.0, 4.0), sleep: Callable[[float], None] = time.sleep) -> None: ...
    def fetch(self, symbols: Sequence[str], start: date, end_utc: datetime, as_of_day: date) -> CompositeFetchResult: ...
    def fetch_daily_bars(self, symbols: Sequence[str], start: date, end_utc: datetime) -> list[Bar]: ...   # = fetch(...).bars (protocol compatibility)
    @staticmethod
    def crosscheck_sample(symbols: Sequence[str], fraction: float, as_of_day: date) -> list[str]: ...    # deterministic rotation: sorted symbols, offset = as_of_day.toordinal() % n
```

- [ ] **Step 1: Failing tests**: `test_parse_time_series_strings_to_floats_and_dates`; `test_429_raises_rate_limit`; `test_bad_symbol_raises_not_retryable`; `test_budget_spent_per_request`; `test_csv_source_filters_range`; `test_composite_uses_primary_when_ok` (fallback never called); `test_composite_retries_then_falls_back` (primary raises retryable 3 times for whole batch -> fallback called per symbol, `fallback_used` quality rows, `sleep` called with 1,2,4); `test_composite_fallback_capped`; `test_composite_crosscheck_flags_mismatch` (fallback returns a close 1 % off -> `close_mismatch` with both values in detail; primary value kept); `test_crosscheck_sample_rotates_daily` (consecutive days give different disjoint-ish samples covering all symbols within ceil(1/fraction) days); `test_crosscheck_skipped_when_budget_low`. Integration: one Twelve Data call for `XOM` with 5 days, skipped without key.
- [ ] **Steps 2-5.** Commit `feat(data): twelve data and csv sources, composite fallback with cross-check`.

---

### Task 11: Analysis pipeline

**Files:**
- Create: `src/uncorrelated_trading/analysis/pipeline.py`, `tests/unit/test_pipeline.py`, `tests/property/test_pipeline_props.py`

**Interfaces:**
- Consumes: Task 5 (`build_price_matrix`, `log_returns`, `correlation_matrix`), Task 6 (`search_uncorrelated_sets`), Task 7 (`momentum_scores`), `models.*`.
- Produces: `def run_analysis(bars: Sequence[Bar], universe: Sequence[str], as_of: AsOf, params: AnalysisParams, now_utc: datetime, quality_in: Sequence[SymbolQuality] = (), run_id: str | None = None) -> AnalysisResult`.

Behaviour: builds price matrix for `max(params.windows, params.momentum_long) + 1` days ending `as_of.day`; drops symbols per Task 5; computes Pearson and Spearman for every window in `params.windows` (each window uses the last `window` rows of the full return matrix; symbols lacking enough history for a longer window are excluded from that window only); momentum on the full matrix; set search on the primary window with `long_pearson` = the longest window's Pearson matrix and `momentum_z` from the composite scores; merges `quality_in` (from the fetch step) with analysis drops into `DataQualityReport`; raises `AnalysisError` when fewer than 3 symbols survive the primary window.

- [ ] **Step 1: Failing tests**: `test_pipeline_end_to_end_on_sample_bars` (10 symbols from `make_bars`, as_of last day: result has 3 windows x 2 methods, pairs non-empty, momentum has one entry per analysed symbol with ranks 1..n, `run_id` is 32 hex chars); `test_symbol_with_gap_is_dropped_and_reported`; `test_too_few_symbols_raises`; `test_quality_in_is_merged`; `test_short_history_symbol_excluded_only_from_long_windows` (symbol with 100 bars appears in the 60-day matrix, not in 120 or 250). Property: for random universes of 4..12 symbols generated by `make_bars` with random seeds, every set in `sets_by_size` and `largest_set` satisfies the pairwise bound against the primary Pearson matrix in `result.correlations`.
- [ ] **Steps 2-5.** Commit `feat(analysis): run_analysis pipeline`.

---

### Task 12: Brief rendering (text and HTML)

**Files:**
- Create: `src/uncorrelated_trading/report/summary.py`, `tests/unit/test_summary.py`, `tests/factories.py` (hand-built `AnalysisResult`, `PortfolioContext` with 2 positions, one in the largest set), `tests/golden/brief.txt`, `tests/golden/brief.html`

**Interfaces:**
- Consumes: `models.AnalysisResult`, `PortfolioContext`, `config.ReportConfig`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class BriefInputs:
    result: AnalysisResult
    portfolio: PortfolioContext
    previous_largest: tuple[str, ...] | None     # for the "changes since last run" line
    report_cfg: ReportConfig
    app_version: str

def subject_line(inputs: BriefInputs) -> str: ...        # "[prefix] Brief for <send day> (data through <as_of.day>, <mode>)"
def render_text(inputs: BriefInputs) -> str: ...
def render_html(inputs: BriefInputs) -> str: ...
def holdings_rows(inputs: BriefInputs) -> list[dict[str, object]]: ...   # shared by workbook: symbol, qty, pl, pl_pct, momentum_rank, direction, in_largest_set
```

Section order and content per A9. Numbers: rho to 3 decimals, returns as percent with 2 decimals, money with thousands separators and 2 decimals. Text width <= 100 characters per line. HTML: a single `<html>` with inline CSS, `<table>` elements, no external URLs or scripts. Footer: parameters, statistical caveat (SE of rho at the primary window), dividend caveat, "advisory output, not investment advice", app version, run_id. If `portfolio.error` is set, the holdings section shows the error text instead of a table.

- [ ] **Step 1: Failing tests**: `test_text_matches_golden` and `test_html_matches_golden` (golden files generated once by the implementer from `factories.py`, then frozen; the test compares exactly); `test_subject_line_format`; `test_html_has_no_external_urls` (regex `https?://` absent except none); `test_holdings_section_shows_error_when_account_unavailable`; `test_changes_line_lists_added_and_removed_symbols`; `test_text_lines_within_width`.
- [ ] **Steps 2-5.** Commit `feat(report): text and html brief rendering`.

---

### Task 13: Workbook builder

**Files:**
- Create: `src/uncorrelated_trading/report/workbook.py`, `tests/unit/test_workbook.py`, `tests/property/test_workbook_props.py`

**Interfaces:**
- Consumes: `models.AnalysisResult`, `PortfolioContext`, `summary.holdings_rows` (Task 12), `summary.BriefInputs`.
- Produces: `def build_workbook(inputs: BriefInputs) -> bytes`, `def workbook_filename(as_of_day: date) -> str` (`uncorrelated_YYYY-MM-DD.xlsx`), and `def tables(inputs: BriefInputs) -> dict[str, list[list[object]]]` (sheet name -> rows including the header row, plain Python types only; the workbook is built from these tables and Task 14 reuses them).

Sheets and columns:
- `Summary`: key/value rows (as-of day, mode, provisional, calendar source, universe size, analysed, run id, created at, tau, windows, largest set size, exact flag, account equity/cash/day P&L).
- `Pearson <w>` and `Spearman <w>` for each window: symbols as both header row and first column, values with number format `0.000`, `ColorScaleRule` (one rule over the numeric range, min -1 red, mid 0 white, max 1 blue), frozen panes at `B2`.
- `Uncorrelated Sets`: size, rank, is_largest, symbols (comma-joined), max_abs_rho, max_abs_rho_ci_upper, mean_abs_rho, min_eigenvalue, spearman_max_abs_rho, passes_spearman, stable_in_long_window, mean_momentum_z.
- `Momentum`: rank, symbol, direction, composite_z, trailing_return (`0.00%`), skip_return, vol_adjusted_return, realized_vol.
- `Positions`: from `holdings_rows` plus account summary rows below.
- `Data Quality`: per-symbol status rows plus counts.
- `Parameters`: every `AnalysisParams` field.
Header rows bold with frozen panes; column widths set from max content length capped at 40; sheet names validated <= 31 chars; built in a `BytesIO`.

- [ ] **Step 1: Failing tests**: `test_workbook_has_expected_sheets` (reload with `openpyxl.load_workbook(BytesIO(...))`, exact sheet name list for 3 windows); `test_correlation_sheet_is_square_with_symbol_headers`; `test_momentum_sheet_rows_match_result`; `test_positions_sheet_flags_largest_set_member`; `test_filename`. Property: for `AnalysisResult`s built from random `make_bars` universes (3..8 symbols) run through `run_analysis`, `build_workbook` succeeds and reload shows `Momentum` row count equals number of analysed symbols.
- [ ] **Steps 2-5.** Commit `feat(report): xlsx workbook builder`.

---

### Task 14: Google Sheets publisher (optional extra)

**Files:**
- Create: `src/uncorrelated_trading/report/sheets.py`, `tests/unit/test_sheets.py`, `docs/modules/sheets.md`

**Interfaces:**
- Consumes: `summary.BriefInputs`, the same table-building helpers as Task 13 (expose in `workbook.py`: `def tables(inputs: BriefInputs) -> dict[str, list[list[object]]]` returning sheet name -> rows including header; Task 13 must provide this; Task 14 consumes it).
- Produces:

```python
class SheetsPublisher:
    def __init__(self, service_account_file: Path, sheet_id: str, client_factory: Callable[[Path], Any] | None = None) -> None: ...   # default factory imports gspread lazily
    def publish(self, inputs: BriefInputs) -> str: ...   # returns https://docs.google.com/spreadsheets/d/<id>; raises ReportError on failure
```

For each table: get or create worksheet by name, `clear()`, `update(values, value_input_option="RAW")`. Values are JSON-safe (floats, ints, str; `None` -> ""). Total write calls <= 2 per sheet. A missing `gspread` import raises `ReportError("gspread not installed; install with `uv sync --extra sheets`")`.

- [ ] **Step 1: Failing tests** with a fake client/spreadsheet/worksheet recording calls: `test_publish_creates_missing_worksheets`; `test_publish_clears_and_updates_each_table`; `test_values_are_json_safe` (numpy floats converted to Python floats, None to ""); `test_missing_gspread_gives_clear_error` (monkeypatch `builtins.__import__` to raise `ImportError` for `gspread`); `test_returns_url`.
- [ ] **Steps 2-5.** Commit `feat(report): optional google sheets publisher`.

---

### Task 15: Jobs and CLI

**Files:**
- Create: `src/uncorrelated_trading/jobs.py`, `src/uncorrelated_trading/cli.py`, `src/uncorrelated_trading/__main__.py`, `src/uncorrelated_trading/app.py` (composition root), `tests/unit/test_jobs.py`, `tests/unit/test_cli.py`, `tests/e2e/test_daily_dry_run.py`

**Interfaces:**
- Consumes: everything from Tasks 2 to 14.
- Produces:

```python
# app.py
@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings; config: AppConfig; clock: Clock; conn: sqlite3.Connection; bars: BarStore; runs: RunStore
    calendar: TradingCalendar; data_source: CompositeDataSource; account: AccountSource; sender: EmailSender; sheets: SheetsPublisher | None
def build_container(settings: Settings, config: AppConfig, *, offline: bool = False, dry_run: bool = False, csv_path: Path | None = None, clock: Clock | None = None) -> Container: ...

# jobs.py
@dataclass(frozen=True, slots=True)
class FetchOutcome: as_of: AsOf; bars_written: int; quality: tuple[SymbolQuality, ...]; fell_back_to_previous_day: bool
def resolve_run_as_of(c: Container, override: date | None) -> AsOf: ...
def fetch_job(c: Container, as_of: AsOf) -> FetchOutcome: ...
def analyze_job(c: Container, as_of: AsOf, quality_in: Sequence[SymbolQuality]) -> AnalysisResult: ...   # persists run + portfolio snapshot (account errors captured in PortfolioContext.error)
def report_job(c: Container, run_id: str, out_dir: Path | None = None) -> ReportBundle: ...            # builds message + workbook; optional Sheets publish; writes xlsx to out_dir if given
def send_job(c: Container, bundle: ReportBundle) -> SendReceipt: ...                                    # logs to email_log; refuses if already sent for run_id unless force
def daily_job(c: Container, as_of_override: date | None = None) -> SendReceipt: ...                    # fetch -> analyze -> report -> send

# cli.py
def main(argv: Sequence[str] | None = None) -> int: ...
```

CLI commands and exit codes per A10. `--as-of` parses `YYYY-MM-DD` and uses `calendar.snap_as_of`. `--dry-run` forces `FileSender` to `<data_dir>/outbox`. `--offline --csv PATH` uses `CsvDataSource` and `NullAccountSource` and calls `Settings.from_env(require_alpaca=False)`; combined with `--dry-run` it also passes `require_email=False`, so the e2e test needs no environment at all. `fetch_job` implements the A6 fallback: in post-close modes, if fewer than half the universe have a bar for `as_of.day`, re-resolve to the previous trading day and set `fell_back_to_previous_day=True`.

- [ ] **Step 1: Failing tests**: `test_fetch_job_incremental_requests_overlap` (pre-seeded store; the fake source records requested start = last - 7 days); `test_fetch_end_respects_sip_rule`; `test_fetch_falls_back_when_today_bars_missing`; `test_analyze_job_persists_and_captures_account_error`; `test_send_job_refuses_duplicate`; `test_send_failure_exit_code_5` (sender raises `EmailSendError`; `main(["send","--latest"])` returns 5 and the run remains loadable); `test_check_config_exit_code_2_on_missing_env`; `test_cli_daily_dry_run_offline_writes_outputs` (e2e: `main(["daily","--offline","--csv",fixture,"--dry-run","--as-of","2026-10-13"])` returns 0; outbox contains one `.eml` parseable by `email.message_from_bytes` with an `.xlsx` attachment that opens in openpyxl with the `Momentum` sheet).
- [ ] **Steps 2-5.** Commit `feat(cli): jobs, composition root and trader command`.

---

### Task 16: Scheduler and `run` command

**Files:**
- Create: `src/uncorrelated_trading/scheduler.py`, `tests/unit/test_scheduler.py`, `tests/property/test_scheduler_props.py`
- Modify: `src/uncorrelated_trading/cli.py` (add `run` subcommand wiring only)

**Interfaces:**
- Consumes: `jobs.daily_job`, `jobs.fetch_job`, `jobs.analyze_job`, `jobs.report_job`, `jobs.send_job`, `RunStore.job_started/job_finished`, `calendar.Clock`, `ScheduleConfig`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class Job:
    name: str
    next_fire: Callable[[datetime], datetime]       # given now_utc -> next fire time (UTC)
    action: Callable[[date], None]                   # receives the local run date
    misfire_grace: timedelta
    run_on_startup_if_missed: bool

def daily_at(local_time: time, tz: str, allowed: Callable[[date], bool]) -> Callable[[datetime], datetime]: ...
class Scheduler:
    def __init__(self, jobs: Sequence[Job], clock: Clock, runs: RunStore, tz: str, poll_seconds: int, stop_event: threading.Event | None = None) -> None: ...
    def tick(self) -> list[str]: ...                 # runs due jobs once; returns names run
    def run_forever(self) -> None: ...               # installs SIGINT/SIGTERM handlers that set stop_event
def build_jobs(c: Container) -> list[Job]: ...      # refresh (fetch+analyze+report at refresh_time on trading days) and send (at send_time on send_on_days; runs daily_job if no unsent report exists)
```

- [ ] **Step 1: Failing tests** with `FakeClock`: `test_daily_at_crosses_dst` (07:00 local on 2026-03-08 and 2026-03-09 differ by 23 h in UTC); `test_tick_runs_job_once_per_date`; `test_restart_does_not_resend` (new `Scheduler` over the same `RunStore` after a run on the same date does not run `send` again); `test_misfire_within_grace_runs`; `test_misfire_beyond_grace_skips_and_logs`; `test_exception_in_job_does_not_stop_loop`; `test_stop_event_ends_run_forever` (thread + event). Property: for random local times and tz, `daily_at` output is strictly after `now` and converts back to the requested local wall time.
- [ ] **Steps 2-5.** Commit `feat(scheduler): stdlib scheduler with idempotent daily jobs`.

---

### Task 17: Documentation, integration CI, GitHub remote

**Files:**
- Create: `README.md` (full), `docs/operations.md` (systemd, launchd, Task Scheduler, cron examples), `docs/configuration.md`, `CONTRIBUTING.md`, `.github/workflows/integration.yml` (finalise), `.github/dependabot.yml` (uv/pip ecosystem monthly)
- Modify: `.github/workflows/ci.yml` (add `--cov-fail-under=85`), `CHANGELOG.md` (0.1.0 entry)

Content requirements for README: purpose and advisory-only statement; architecture diagram in ASCII; prerequisites (`uv`, Python 3.11+); account setup walkthroughs (Alpaca paper-only signup and key creation; Twelve Data key; Gmail 2-step verification and app password; Resend key, the note that unverified Resend accounts can only send to the account owner's address from `onboarding@resend.dev`; Google Cloud service account and sheet sharing); quick start commands; as-of semantics table (five modes); every CLI command with examples; config reference linking `docs/configuration.md`; data notes (split-adjusted, SIP 15-minute rule, rate budgets); warning not to place `data/` on a cloud-synced folder (SQLite WAL); troubleshooting (exit codes, common errors); testing and CI; license.

- [ ] **Step 1**: write docs. **Step 2**: `uv run pytest -q --cov --cov-fail-under=85` passes locally; fix any coverage gaps by adding tests in the owning test files (not by excluding code). **Step 3**: `gh auth status`; `gh repo create uncorrelated-trading --private --source=. --remote=origin --push`; confirm the `ci` workflow runs green on all 6 matrix cells via `gh run watch`. If `gh` is not authenticated, stop and report; do not attempt browser login. **Step 4**: Commit `docs: full README, operations guide, integration workflow`.

---

### Task 18: Final verification (lead)

- [ ] Fresh clone into the scratchpad directory, `uv sync --all-extras`, run the full non-integration suite on Python 3.11 and 3.12 (`uv run --python 3.11 pytest`), `ruff`, `mypy`.
- [ ] `uv run trader check-config` with a `.env` built from `.env.example` placeholders: expect exit 2 listing placeholders as invalid; with real paper keys (if the user has supplied them): expect exit 0.
- [ ] `uv run trader daily --offline --csv tests/fixtures/csv/bars_small.csv --dry-run --as-of 2026-10-13`: open the produced `.eml` and `.xlsx`, read the brief for sense (tables aligned, numbers plausible, caveats present).
- [ ] If Alpaca paper keys are present: `uv run trader fetch` then `uv run trader analyze` then `uv run trader report --latest --out /tmp-equivalent-in-scratchpad` and inspect; `uv run trader send --latest --dry-run`.
- [ ] Grep the tree for forbidden things: `submit_order`, `OrderRequest`, `os.path`, naive `datetime.now()` without tz, `print(` in `src/`, any real-looking key pattern `PK[A-Z0-9]{18}` or `SK[A-Za-z0-9]{30,}` in tracked files.
- [ ] Whole-branch review against Part A, section by section; record gaps as GitHub issues; tag `v0.1.0`.

## Verification summary (how the user confirms the result)

1. `uv sync --all-extras && uv run pytest -q` passes with coverage >= 85 %, `uv run ruff check .`, `uv run mypy src` clean.
2. `uv run trader daily --offline --csv tests/fixtures/csv/bars_small.csv --dry-run` writes an `.eml` and `.xlsx` into `data/outbox/`.
3. With `.env` filled in: `uv run trader check-config` exits 0; `uv run trader daily --dry-run` fetches real Alpaca bars and writes the outbox files; `uv run trader daily` delivers the email; `uv run trader run` stays up and sends at 07:00 the next morning exactly once.
4. GitHub Actions `ci` is green on ubuntu, macos and windows for Python 3.11 and 3.12.

## Self-review notes

- Spec coverage: A4 -> T1; A5 -> T2; A6 -> T3 (+T9 calendar fetch, T15 fallback); A7 -> T4, T9, T10, T15; A8 -> T5, T6, T7, T11; A9 -> T8, T12, T13, T14; A10 -> T15, T16; A11 -> T1, T17, every task's tests; A12 -> T17.
- Type consistency: `fetch_daily_bars(symbols, start: date, end_utc: datetime)` is used by Tasks 9, 10, 15; `BriefInputs` is shared by Tasks 12, 13, 14; `AsOf` fields (`provisional`, `calendar_source`) match Task 3 tests and Task 4 schema; `UncorrelatedSet` fields match Task 6 diagnostics and Task 4 schema.
- Cross-task dependencies that are not plain imports: Task 13 exposes `tables()` for Task 14; Task 3's `CachedCalendar` is tested against a fake store and wired to the real `BarStore` in Task 15.
