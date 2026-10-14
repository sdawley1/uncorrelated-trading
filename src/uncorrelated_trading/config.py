"""Runtime settings from the environment and application config from TOML.

Two sources of configuration are kept apart:

* :class:`Settings` holds secrets and machine-specific values read from environment
  variables, optionally supplemented by a ``.env`` file (python-dotenv). Values already
  present in the process environment always win over the file.
* :class:`AppConfig` holds the non-secret analysis, data, schedule and report parameters
  read from a TOML file: ``UT_CONFIG_FILE`` (or ``--config``) when set, else
  ``./config/default.toml`` when it exists, else the copy shipped inside the package
  (``uncorrelated_trading/config_default.toml``, see :func:`packaged_config_path`). Missing
  sections or keys fall back to the defaults of spec section A5, so a partial TOML file is
  valid.

Both loaders collect every problem they find and raise a single :class:`ConfigError`
listing all of them.
"""

from __future__ import annotations

import enum
import fnmatch
import os
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from uncorrelated_trading.errors import ConfigError
from uncorrelated_trading.models import AnalysisParams

# =============================================================================
# Constants
# =============================================================================

EmailBackend = Literal["smtp", "resend", "file"]
SendOnDays = Literal["weekdays", "trading_days", "every_day"]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_EMAIL_BACKENDS: tuple[EmailBackend, ...] = ("smtp", "resend", "file")
_SEND_ON_DAYS: tuple[SendOnDays, ...] = ("weekdays", "trading_days", "every_day")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SECRET_FIELDS = frozenset(
    {"alpaca_api_key", "alpaca_secret_key", "twelvedata_api_key", "smtp_password", "resend_api_key"}
)
_DB_FILENAME = "uncorrelated_trading.sqlite3"
_LOCAL_CONFIG = Path("config/default.toml")
_PACKAGED_CONFIG = "config_default.toml"
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
# ``.env.example`` placeholders: a run of 8+ X (or x), optionally after a short key prefix such
# as ``PK`` or ``re_``; and ``/path/to/...`` file paths.
_PLACEHOLDER_RE = re.compile(r"^(?:[A-Za-z]{1,2}_?)?(?:X{8,}|x{8,})$")
_PLACEHOLDER_PATH_PREFIX = "/path/to/"
_ALPACA_SECRETS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
_EMAIL_SECRETS = ("SMTP_PASSWORD", "RESEND_API_KEY")
_OPTIONAL_SECRETS = ("TWELVEDATA_API_KEY", "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE")
_MIN_SYMBOLS = 2
_MAX_SYMBOLS = 200


def packaged_config_path() -> Path:
    """Return the path of the default TOML config shipped inside the package.

    Returns
    -------
    Path
        ``uncorrelated_trading/config_default.toml`` in the installed package; a byte-identical
        copy of the repository's ``config/default.toml``.
    """
    return Path(str(resources.files("uncorrelated_trading") / _PACKAGED_CONFIG))


def default_config_path() -> Path:
    """Return the config file used when neither ``UT_CONFIG_FILE`` nor ``--config`` is set.

    Returns
    -------
    Path
        ``config/default.toml`` (relative to the working directory) when that file exists,
        else :func:`packaged_config_path`.
    """
    return _LOCAL_CONFIG if _LOCAL_CONFIG.is_file() else packaged_config_path()


def is_placeholder(value: str) -> bool:
    """Return True when ``value`` looks like a placeholder copied from ``.env.example``.

    Parameters
    ----------
    value : str
        Stripped environment value.

    Returns
    -------
    bool
        True for a run of at least 8 ``X`` (or ``x``) characters, optionally after a short
        prefix such as ``PK`` or ``re_`` (``PKXXXXXXXX``), and for paths starting with
        ``/path/to/``.
    """
    return value.startswith(_PLACEHOLDER_PATH_PREFIX) or bool(_PLACEHOLDER_RE.match(value))


class _Unset(enum.Enum):
    """Sentinel type: argument omitted (distinct from an explicit ``None``)."""

    TOKEN = 0


_DEFAULT_DOTENV = _Unset.TOKEN

_DEFAULT_SYMBOLS = tuple(
    """
    XOM CVX COP EOG OXY DVN FANG APA CTRA EQT AR RRC
    SLB HAL BKR MPC PSX VLO KMI WMB OKE LNG TRGP
    FSLR ENPH
    FCX NEM SCCO AA CLF NUE STLD RS MP ALB SQM
    AEM WPM RGLD HL CDE PAAS VALE RIO BHP
    ADM BG CF MOS NTR CTVA FMC DE AGCO TSN INGR DAR
    LIN APD DD DOW LYB CE EMN VMC MLM
    XLE XOP OIH XME GDX GDXJ SIL MOO XLB COPX URA TAN ICLN PICK
    """.split()
)
_DEFAULT_EXCLUDED = tuple("USO UNG GLD SLV DBC BNO UCO SCO PPLT PALL CORN WEAT SOYB CPER".split())

# Defaults of spec A5 (``config/default.toml``), in raw TOML form.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "universe": {
        "symbols": list(_DEFAULT_SYMBOLS),
        "excluded_patterns": list(_DEFAULT_EXCLUDED),
    },
    "data": {
        "backfill_calendar_days": 420,
        "overlap_calendar_days": 7,
        "adjustment": "split",
        "alpaca_feed": "sip",
        "alpaca_requests_per_minute": 180,
        "alpaca_symbols_per_request": 40,
        "twelvedata_requests_per_minute": 7,
        "twelvedata_requests_per_day": 750,
        "twelvedata_fallback_max_per_run": 20,
        "crosscheck_fraction": 0.2,
        "close_mismatch_tolerance": 0.005,
        "post_close_settle_minutes": 20,
    },
    "analysis": {
        "primary_window": 60,
        "windows": [60, 120, 250],
        "tau": 0.20,
        "tau_spearman": 0.30,
        "max_set_size_exact": 4,
        "max_sets_per_size": 50,
        "search_time_budget_s": 20.0,
        # Accepted and range-checked for spec compatibility; not part of AnalysisParams.
        "min_history_fraction": 1.0,
        "momentum_k": 20,
        "momentum_long": 120,
        "momentum_skip": 20,
        "momentum_weights": [0.4, 0.3, 0.3],
        "direction_threshold": 0.5,
    },
    "schedule": {
        "refresh_time": "16:25",
        "send_time": "07:00",
        "send_on_days": "weekdays",
        "poll_seconds": 30,
        "send_misfire_grace_hours": 3,
        "refresh_misfire_grace_hours": 6,
    },
    "report": {
        "top_pairs": 15,
        "top_sets_per_size": 5,
        "top_momentum": 10,
        "subject_prefix": "[uncorrelated-trading]",
    },
}

# =============================================================================
# Settings (environment / .env)
# =============================================================================


@dataclass(frozen=True, slots=True)
class Settings:
    """Secrets and machine-specific settings read from the environment.

    Build instances with :meth:`from_env`. ``repr`` masks every key and password field.
    """

    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool
    twelvedata_api_key: str | None
    email_backend: EmailBackend
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
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | None | _Unset = _DEFAULT_DOTENV,
        *,
        require_alpaca: bool = True,
        require_email: bool = True,
    ) -> Settings:
        """Read settings from environment variables and an optional ``.env`` file.

        Parameters
        ----------
        env : Mapping[str, str] or None
            Variables to read; ``None`` means ``os.environ``. These always take precedence
            over values from the ``.env`` file.
        dotenv_path : Path or None, optional
            ``.env`` file supplying values absent from ``env``. When omitted,
            ``Path.cwd() / ".env"`` is used if it exists; an explicit ``None`` disables
            file loading.
        require_alpaca : bool
            When False, missing Alpaca keys become ``""`` instead of errors.
        require_email : bool
            When False, missing email settings are not errors; an absent ``EMAIL_BACKEND``
            becomes ``"file"``, ``EMAIL_FROM`` ``""`` and ``EMAIL_TO`` ``()``.

        Returns
        -------
        Settings
            Validated settings.

        Raises
        ------
        ConfigError
            Listing every missing or malformed variable at once.
        """
        values = _merge_sources(os.environ if env is None else env, dotenv_path)
        return _SettingsParser(values, require_alpaca, require_email).parse()

    @property
    def sheets_enabled(self) -> bool:
        """Return True when both Google service-account file and sheet id are set."""
        return self.google_service_account_file is not None and bool(self.google_sheet_id)

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path inside ``data_dir``."""
        return self.data_dir / _DB_FILENAME

    def __repr__(self) -> str:
        """Return a representation with every secret field masked as ``***``."""
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            shown = "***" if f.name in _SECRET_FIELDS and value else repr(value)
            parts.append(f"{f.name}={shown}")
        return f"Settings({', '.join(parts)})"


def _merge_sources(env: Mapping[str, str], dotenv_path: Path | None | _Unset) -> dict[str, str]:
    """Merge ``.env`` file values under ``env`` (``env`` wins)."""
    path: Path | None
    if isinstance(dotenv_path, _Unset):
        candidate = Path.cwd() / ".env"
        path = candidate if candidate.is_file() else None
    else:
        path = dotenv_path
    merged: dict[str, str] = {}
    if path is not None and path.is_file():
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(env)
    return merged


class _SettingsParser:
    """Collects problems while reading :class:`Settings` fields from a flat mapping."""

    def __init__(
        self, values: Mapping[str, str], require_alpaca: bool, require_email: bool
    ) -> None:
        self.values = values
        self.require_alpaca = require_alpaca
        self.require_email = require_email
        self.problems: list[str] = []

    def get(self, key: str) -> str | None:
        """Return the stripped value of ``key``, or None when absent or blank."""
        raw = self.values.get(key)
        if raw is None or not raw.strip():
            return None
        return raw.strip()

    def required(self, key: str, needed: bool, reason: str = "") -> str | None:
        """Return ``key``'s value, recording a problem if it is missing and ``needed``."""
        value = self.get(key)
        if value is None and needed:
            self.problems.append(f"{key} is required{reason} but not set")
        return value

    def reject_placeholders(self, keys: tuple[str, ...]) -> set[str]:
        """Record a problem for each of ``keys`` whose value is an ``.env.example`` placeholder.

        Returns the keys that were rejected.
        """
        rejected: set[str] = set()
        for key in keys:
            value = self.get(key)
            if value is not None and is_placeholder(value):
                self.problems.append(f"{key} looks like a placeholder from .env.example")
                rejected.add(key)
        return rejected

    def boolean(self, key: str, default: bool) -> bool:
        """Parse a boolean variable (``1/true/yes/on`` or ``0/false/no/off``)."""
        raw = self.get(key)
        if raw is None:
            return default
        if raw.lower() in _TRUE_VALUES:
            return True
        if raw.lower() in _FALSE_VALUES:
            return False
        self.problems.append(f"{key} must be a boolean (true/false), got {raw!r}")
        return default

    def integer(self, key: str, default: int) -> int:
        """Parse a positive integer variable."""
        raw = self.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value <= 0:
            self.problems.append(f"{key} must be a positive integer, got {raw!r}")
            return default
        return value

    def email_settings(self) -> tuple[EmailBackend, str, tuple[str, ...]]:
        """Read and validate ``EMAIL_BACKEND``, ``EMAIL_FROM`` and ``EMAIL_TO``."""
        raw_backend = self.required("EMAIL_BACKEND", self.require_email)
        backend: EmailBackend = "file"
        if raw_backend is not None:
            if raw_backend.lower() in _EMAIL_BACKENDS:
                backend = cast(EmailBackend, raw_backend.lower())
            else:
                self.problems.append(
                    f"EMAIL_BACKEND must be one of {', '.join(_EMAIL_BACKENDS)}, "
                    f"got {raw_backend!r}"
                )
        sender = self.required("EMAIL_FROM", self.require_email) or ""
        if sender and "@" not in sender:
            self.problems.append(f"EMAIL_FROM is not an email address: {sender!r}")
        raw_to = self.required("EMAIL_TO", self.require_email) or ""
        recipients = tuple(a.strip() for a in raw_to.split(",") if a.strip())
        if raw_to and not recipients:
            self.problems.append("EMAIL_TO must list at least one address")
        bad = [a for a in recipients if "@" not in a]
        if bad:
            self.problems.append(f"EMAIL_TO contains invalid addresses: {', '.join(bad)}")
        return backend, sender, recipients

    def config_file(self) -> Path:
        """Read ``UT_CONFIG_FILE``, defaulting to :func:`default_config_path`."""
        raw = self.get("UT_CONFIG_FILE")
        return Path(raw) if raw else default_config_path()

    def timezone(self) -> str:
        """Read ``UT_TIMEZONE`` and check it names a known IANA zone."""
        tz = self.get("UT_TIMEZONE") or "America/New_York"
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            self.problems.append(f"UT_TIMEZONE is not a known IANA timezone: {tz!r}")
        return tz

    def log_level(self) -> str:
        """Read ``LOG_LEVEL`` (case-insensitive) and check it is a stdlib level name."""
        level = (self.get("LOG_LEVEL") or "INFO").upper()
        if level not in _LOG_LEVELS:
            self.problems.append(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
            return "INFO"
        return level

    def parse(self) -> Settings:
        """Build :class:`Settings` or raise :class:`ConfigError` with every problem."""
        api_key = self.required("ALPACA_API_KEY", self.require_alpaca) or ""
        secret_key = self.required("ALPACA_SECRET_KEY", self.require_alpaca) or ""
        backend, sender, recipients = self.email_settings()
        smtp_needed = self.require_email and backend == "smtp"
        resend_needed = self.require_email and backend == "resend"
        smtp_username = self.required("SMTP_USERNAME", smtp_needed, " for EMAIL_BACKEND=smtp")
        smtp_password = self.required("SMTP_PASSWORD", smtp_needed, " for EMAIL_BACKEND=smtp")
        resend_key = self.required("RESEND_API_KEY", resend_needed, " for EMAIL_BACKEND=resend")
        sa_file = self.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        keys: tuple[str, ...] = _OPTIONAL_SECRETS
        if self.require_alpaca:
            keys = (*_ALPACA_SECRETS, *keys)
        if self.require_email:
            keys = (*keys, *_EMAIL_SECRETS)
        rejected = self.reject_placeholders(keys)
        sheet_id = self.get("GOOGLE_SHEET_ID")
        if (
            sa_file is not None
            and sheet_id is not None
            and "GOOGLE_SERVICE_ACCOUNT_FILE" not in rejected
            and not Path(sa_file).is_file()
        ):
            self.problems.append(f"GOOGLE_SERVICE_ACCOUNT_FILE does not exist: {sa_file}")
        settings = Settings(
            alpaca_api_key=api_key,
            alpaca_secret_key=secret_key,
            alpaca_paper=self.boolean("ALPACA_PAPER", True),
            twelvedata_api_key=self.get("TWELVEDATA_API_KEY"),
            email_backend=backend,
            email_from=sender,
            email_to=recipients,
            smtp_host=self.get("SMTP_HOST") or "smtp.gmail.com",
            smtp_port=self.integer("SMTP_PORT", 587),
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_use_ssl=self.boolean("SMTP_USE_SSL", False),
            resend_api_key=resend_key,
            google_service_account_file=Path(sa_file) if sa_file else None,
            google_sheet_id=self.get("GOOGLE_SHEET_ID"),
            data_dir=Path(self.get("UT_DATA_DIR") or "data"),
            config_file=self.config_file(),
            timezone=self.timezone(),
            log_level=self.log_level(),
        )
        if self.problems:
            raise ConfigError(self.problems)
        return settings


# =============================================================================
# AppConfig (TOML)
# =============================================================================


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    """Symbols to analyse and fnmatch patterns that must never appear among them."""

    symbols: tuple[str, ...]
    excluded_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataConfig:
    """Market-data fetch, rate-limit and cross-validation parameters."""

    backfill_calendar_days: int
    overlap_calendar_days: int
    adjustment: str
    alpaca_feed: str
    alpaca_requests_per_minute: int
    alpaca_symbols_per_request: int
    twelvedata_requests_per_minute: int
    twelvedata_requests_per_day: int
    twelvedata_fallback_max_per_run: int
    crosscheck_fraction: float
    close_mismatch_tolerance: float
    post_close_settle_minutes: int


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """Scheduler times (exchange-local) and misfire handling."""

    refresh_time: time
    send_time: time
    send_on_days: SendOnDays
    poll_seconds: int
    send_misfire_grace_hours: int
    refresh_misfire_grace_hours: int


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """Report sizing and the email subject prefix."""

    top_pairs: int
    top_sets_per_size: int
    top_momentum: int
    subject_prefix: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete non-secret application configuration."""

    universe: UniverseConfig
    data: DataConfig
    analysis: AnalysisParams
    schedule: ScheduleConfig
    report: ReportConfig

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Load and validate a TOML config file.

        Missing sections and keys fall back to the spec A5 defaults. Unknown sections or
        keys, wrong types and out-of-range values are all reported together.

        Parameters
        ----------
        path : Path
            TOML file to read.

        Returns
        -------
        AppConfig
            Validated configuration.

        Raises
        ------
        ConfigError
            If the file is missing or unparsable, or listing every validation problem.
        """
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError([f"config file not found: {path}"]) from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError([f"invalid TOML in {path}: {exc}"]) from exc
        return _build_app_config(raw)


class _Section:
    """Typed, problem-collecting accessor for one TOML section merged over defaults."""

    def __init__(self, name: str, raw: Mapping[str, Any], problems: list[str]) -> None:
        self.name = name
        self.problems = problems
        defaults = _DEFAULTS[name]
        for key in raw:
            if key not in defaults:
                problems.append(f"[{name}] unknown key {key!r}")
        self.values: dict[str, Any] = {
            **defaults,
            **{k: v for k, v in raw.items() if k in defaults},
        }

    def _bad(self, key: str, expected: str) -> None:
        """Record a type or range problem for ``key``."""
        self.problems.append(f"[{self.name}] {key} must be {expected}, got {self.values[key]!r}")

    def _typed(self, key: str, check: Callable[[Any], bool], expected: str) -> Any:
        """Return the value of ``key`` if ``check`` passes, else record and use the default."""
        value = self.values[key]
        if check(value):
            return value
        self._bad(key, expected)
        return _DEFAULTS[self.name][key]

    def integer(self, key: str, minimum: int = 1) -> int:
        """Return an integer ``>= minimum``."""
        return int(
            self._typed(key, lambda v: _is_int(v) and v >= minimum, f"an integer >= {minimum}")
        )

    def number(self, key: str, low: float, high: float, *, low_open: bool = False) -> float:
        """Return a number in ``[low, high]`` (or ``(low, high)`` when ``low_open``)."""
        bracket = "(" if low_open else "["
        expected = f"a number in {bracket}{low}, {high}{')' if low_open else ']'}"

        def ok(v: Any) -> bool:
            if not _is_number(v):
                return False
            above = v > low if low_open else v >= low
            below = v < high if low_open else v <= high
            return bool(above and below)

        return float(self._typed(key, ok, expected))

    def string(self, key: str, choices: tuple[str, ...] | None = None) -> str:
        """Return a non-empty string, optionally restricted to ``choices``."""
        if choices is None:
            return str(self._typed(key, lambda v: isinstance(v, str) and v != "", "a string"))
        return str(self._typed(key, lambda v: v in choices, f"one of {', '.join(choices)}"))

    def str_list(self, key: str) -> tuple[str, ...]:
        """Return a list of strings as a tuple."""
        value = self._typed(
            key,
            lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
            "a list of strings",
        )
        return tuple(value)

    def int_list(self, key: str) -> tuple[int, ...]:
        """Return a non-empty list of positive integers as a tuple."""
        value = self._typed(
            key,
            lambda v: isinstance(v, list) and len(v) > 0 and all(_is_int(x) and x > 0 for x in v),
            "a non-empty list of positive integers",
        )
        return tuple(int(x) for x in value)

    def float_list(self, key: str, length: int) -> tuple[float, ...]:
        """Return a list of ``length`` non-negative numbers as a tuple."""
        value = self._typed(
            key,
            lambda v: (
                isinstance(v, list)
                and len(v) == length
                and all(_is_number(x) and x >= 0 for x in v)
            ),
            f"a list of {length} non-negative numbers",
        )
        return tuple(float(x) for x in value)

    def clock_time(self, key: str) -> time:
        """Return a ``HH:MM`` string parsed as :class:`datetime.time`."""
        value = self.values[key]
        match = _HHMM_RE.match(value) if isinstance(value, str) else None
        if match is not None:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour < 24 and minute < 60:
                return time(hour, minute)
        self._bad(key, "a time 'HH:MM'")
        h, m = _DEFAULTS[self.name][key].split(":")
        return time(int(h), int(m))


def _is_int(value: Any) -> bool:
    """Return True for ints that are not bools."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """Return True for ints or floats that are not bools."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _section(raw: Mapping[str, Any], name: str, problems: list[str]) -> _Section:
    """Return the accessor for section ``name``, tolerating a missing section."""
    body = raw.get(name, {})
    if not isinstance(body, dict):
        problems.append(f"[{name}] must be a table")
        body = {}
    return _Section(name, body, problems)


def _universe(sec: _Section) -> UniverseConfig:
    """Validate the universe: unique, upper-case, 2 to 200 symbols, none excluded."""
    symbols = sec.str_list("symbols")
    patterns = sec.str_list("excluded_patterns")
    problems = sec.problems
    if not _MIN_SYMBOLS <= len(symbols) <= _MAX_SYMBOLS:
        problems.append(
            f"[universe] symbols must contain {_MIN_SYMBOLS} to {_MAX_SYMBOLS} entries, "
            f"got {len(symbols)}"
        )
    dupes = sorted(s for s, n in Counter(symbols).items() if n > 1)
    if dupes:
        problems.append(f"[universe] duplicate symbols: {', '.join(dupes)}")
    bad = [s for s in symbols if not _SYMBOL_RE.match(s)]
    if bad:
        problems.append(f"[universe] symbols must be upper-case tickers: {', '.join(bad)}")
    excluded = [s for s in symbols if any(fnmatch.fnmatchcase(s, p) for p in patterns)]
    if excluded:
        problems.append(f"[universe] symbols match excluded_patterns: {', '.join(excluded)}")
    return UniverseConfig(symbols=symbols, excluded_patterns=patterns)


def _data(sec: _Section) -> DataConfig:
    """Validate the data section."""
    return DataConfig(
        backfill_calendar_days=sec.integer("backfill_calendar_days"),
        overlap_calendar_days=sec.integer("overlap_calendar_days", 0),
        adjustment=sec.string("adjustment", ("raw", "split", "dividend", "all")),
        alpaca_feed=sec.string("alpaca_feed", ("sip", "iex")),
        alpaca_requests_per_minute=sec.integer("alpaca_requests_per_minute"),
        alpaca_symbols_per_request=sec.integer("alpaca_symbols_per_request"),
        twelvedata_requests_per_minute=sec.integer("twelvedata_requests_per_minute"),
        twelvedata_requests_per_day=sec.integer("twelvedata_requests_per_day"),
        twelvedata_fallback_max_per_run=sec.integer("twelvedata_fallback_max_per_run", 0),
        crosscheck_fraction=sec.number("crosscheck_fraction", 0.0, 1.0),
        close_mismatch_tolerance=sec.number("close_mismatch_tolerance", 0.0, 1.0),
        post_close_settle_minutes=sec.integer("post_close_settle_minutes", 0),
    )


def _analysis(sec: _Section) -> AnalysisParams:
    """Validate the analysis section into :class:`AnalysisParams`."""
    params = AnalysisParams(
        primary_window=sec.integer("primary_window", 2),
        windows=sec.int_list("windows"),
        tau=sec.number("tau", 0.0, 1.0, low_open=True),
        tau_spearman=sec.number("tau_spearman", 0.0, 1.0, low_open=True),
        max_set_size_exact=sec.integer("max_set_size_exact", 2),
        max_sets_per_size=sec.integer("max_sets_per_size"),
        search_time_budget_s=sec.number("search_time_budget_s", 0.0, float("inf"), low_open=True),
        momentum_k=sec.integer("momentum_k"),
        momentum_long=sec.integer("momentum_long"),
        momentum_skip=sec.integer("momentum_skip"),
        momentum_weights=cast(tuple[float, float, float], sec.float_list("momentum_weights", 3)),
        direction_threshold=sec.number("direction_threshold", 0.0, float("inf")),
    )
    sec.number("min_history_fraction", 0.0, 1.0)
    if params.primary_window not in params.windows:
        sec.problems.append(
            f"[analysis] primary_window {params.primary_window} must be one of windows "
            f"{list(params.windows)}"
        )
    if params.momentum_long <= params.momentum_skip:
        sec.problems.append(
            f"[analysis] momentum_long ({params.momentum_long}) must exceed momentum_skip "
            f"({params.momentum_skip})"
        )
    if sum(params.momentum_weights) <= 0:
        sec.problems.append("[analysis] momentum_weights must not all be zero")
    return params


def _schedule(sec: _Section) -> ScheduleConfig:
    """Validate the schedule section."""
    return ScheduleConfig(
        refresh_time=sec.clock_time("refresh_time"),
        send_time=sec.clock_time("send_time"),
        send_on_days=cast(SendOnDays, sec.string("send_on_days", _SEND_ON_DAYS)),
        poll_seconds=sec.integer("poll_seconds"),
        send_misfire_grace_hours=sec.integer("send_misfire_grace_hours", 0),
        refresh_misfire_grace_hours=sec.integer("refresh_misfire_grace_hours", 0),
    )


def _report(sec: _Section) -> ReportConfig:
    """Validate the report section."""
    return ReportConfig(
        top_pairs=sec.integer("top_pairs"),
        top_sets_per_size=sec.integer("top_sets_per_size"),
        top_momentum=sec.integer("top_momentum"),
        subject_prefix=str(sec._typed("subject_prefix", lambda v: isinstance(v, str), "a string")),
    )


def _build_app_config(raw: Mapping[str, Any]) -> AppConfig:
    """Validate parsed TOML and build :class:`AppConfig`, raising once with all problems."""
    problems: list[str] = [f"unknown section [{name}]" for name in raw if name not in _DEFAULTS]
    config = AppConfig(
        universe=_universe(_section(raw, "universe", problems)),
        data=_data(_section(raw, "data", problems)),
        analysis=_analysis(_section(raw, "analysis", problems)),
        schedule=_schedule(_section(raw, "schedule", problems)),
        report=_report(_section(raw, "report", problems)),
    )
    if problems:
        raise ConfigError(problems)
    return config
