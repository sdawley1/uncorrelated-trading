from __future__ import annotations

import os
from pathlib import Path

import pytest

from uncorrelated_trading import app, cli, jobs
from uncorrelated_trading.clock import Clock
from uncorrelated_trading.config import Settings
from uncorrelated_trading.errors import AnalysisError, DataSourceError, EmailSendError
from uncorrelated_trading.models import EmailMessage, SendReceipt
from uncorrelated_trading.store.db import connect
from uncorrelated_trading.store.runs import RunStore

REPO = Path(__file__).resolve().parents[2]
OFFLINE_TOML = REPO / "tests" / "fixtures" / "config" / "offline.toml"
CSV = REPO / "tests" / "fixtures" / "csv" / "bars_small.csv"
DB_NAME = "uncorrelated_trading.sqlite3"


@pytest.fixture(autouse=True)
def empty_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def base(tmp_path: Path) -> list[str]:
    return ["--config", str(OFFLINE_TOML), "--data-dir", str(tmp_path)]


def offline_daily(tmp_path: Path) -> int:
    return cli.main(
        [
            *base(tmp_path),
            "daily",
            "--offline",
            "--csv",
            str(CSV),
            "--dry-run",
            "--as-of",
            "2026-10-13",
        ]
    )


class FailingSender:
    name = "smtp"

    def send(self, message: EmailMessage) -> SendReceipt:
        raise EmailSendError("smtp", "535 authentication failed", retryable=False)


def test_check_config_exit_code_2_on_missing_env(tmp_path: Path):
    assert cli.main([*base(tmp_path), "check-config"]) == 2


def test_check_config_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("ALPACA_API_KEY", "PKFAKEFAKEFAKE")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("EMAIL_BACKEND", "file")
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")
    assert cli.main([*base(tmp_path), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "10 symbols" in out
    assert "fake-secret" not in out


def test_missing_config_file_exit_code_2(tmp_path: Path):
    argv = [
        "--config",
        str(tmp_path / "nope.toml"),
        "--data-dir",
        str(tmp_path),
        "daily",
        "--offline",
        "--csv",
        str(CSV),
        "--dry-run",
    ]
    assert cli.main(argv) == 2


def test_bad_arguments_exit_code_2(tmp_path: Path):
    assert cli.main([*base(tmp_path), "daily", "--as-of", "13/10/2026"]) == 2
    assert cli.main([*base(tmp_path), "send"]) == 2  # --run-id or --latest required
    assert cli.main(["no-such-command"]) == 2


def test_offline_requires_csv(tmp_path: Path):
    assert cli.main([*base(tmp_path), "fetch", "--offline"]) == 2


def test_run_is_placeholder(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main([*base(tmp_path), "run"]) == 0
    assert "scheduler not yet available" in capsys.readouterr().out


def test_calendar_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main([*base(tmp_path), "calendar", "--date", "2026-11-26"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-26" in out
    assert "not a trading day" in out
    assert "static" in out


def test_cli_daily_dry_run_offline_writes_outputs(tmp_path: Path):
    assert offline_daily(tmp_path) == 0
    emls = list((tmp_path / "outbox").glob("*.eml"))
    assert len(emls) == 1
    assert (tmp_path / DB_NAME).is_file()


def test_fetch_analyze_report_steps(tmp_path: Path):
    offline = ["--offline", "--csv", str(CSV)]
    assert cli.main([*base(tmp_path), "fetch", *offline, "--as-of", "2026-10-13"]) == 0
    assert cli.main([*base(tmp_path), "analyze", *offline, "--as-of", "2026-10-13"]) == 0
    out_dir = tmp_path / "out"
    assert cli.main([*base(tmp_path), "report", "--latest", "--out", str(out_dir)]) == 0
    assert (out_dir / "uncorrelated_2026-10-13.xlsx").is_file()


def test_report_unknown_run_exit_code_3(tmp_path: Path):
    assert cli.main([*base(tmp_path), "report", "--run-id", "nope"]) == 3


def test_report_latest_without_runs_exit_code_3(tmp_path: Path):
    assert cli.main([*base(tmp_path), "report", "--latest"]) == 3


def test_send_failure_exit_code_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert offline_daily(tmp_path) == 0
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "fake-app-password")

    def failing(settings: Settings, clock: Clock) -> FailingSender:
        return FailingSender()

    monkeypatch.setattr(app, "make_sender", failing)

    assert cli.main([*base(tmp_path), "send", "--latest"]) == 5

    conn = connect(tmp_path / DB_NAME)
    try:
        runs = RunStore(conn)
        run_id = runs.latest_run_id()
        assert run_id is not None
        assert runs.load_analysis(run_id).run_id == run_id
        assert not runs.email_sent_for(run_id)
    finally:
        conn.close()


def test_send_dry_run_writes_outbox(tmp_path: Path):
    assert offline_daily(tmp_path) == 0
    assert cli.main([*base(tmp_path), "send", "--latest", "--dry-run"]) == 0
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1  # same key overwrites


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (DataSourceError("alpaca", "boom", retryable=True), 3),
        (AnalysisError("too few symbols"), 4),
        (EmailSendError("resend", "429", retryable=True), 5),
        (RuntimeError("unexpected"), 1),
    ],
)
def test_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int):
    def boom(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(jobs, "daily_job", boom)
    assert offline_daily(tmp_path) == code


def _settings(tmp_path: Path, **extra: str) -> Settings:
    env = {
        "UT_DATA_DIR": str(tmp_path),
        "ALPACA_API_KEY": "PKFAKEFAKEFAKE",
        "ALPACA_SECRET_KEY": "fake-secret",
        "EMAIL_BACKEND": "file",
        "EMAIL_FROM": "bot@example.com",
        "EMAIL_TO": "me@example.com",
        **extra,
    }
    return Settings.from_env(env, dotenv_path=None)


def test_build_container_online_wiring(tmp_path: Path):
    from uncorrelated_trading.broker.alpaca_account import AlpacaAccountSource
    from uncorrelated_trading.calendar import CachedCalendar
    from uncorrelated_trading.config import AppConfig
    from uncorrelated_trading.report.email.file_sender import FileSender
    from uncorrelated_trading.report.sheets import SheetsPublisher

    key_file = tmp_path / "sa.json"
    key_file.write_text("{}", encoding="utf-8")
    settings = _settings(
        tmp_path,
        TWELVEDATA_API_KEY="fake-td-key",
        GOOGLE_SERVICE_ACCOUNT_FILE=str(key_file),
        GOOGLE_SHEET_ID="sheet-id",
    )
    c = app.build_container(settings, AppConfig.load(OFFLINE_TOML))
    try:
        assert isinstance(c.account, AlpacaAccountSource)
        assert isinstance(c.calendar, CachedCalendar)
        assert isinstance(c.sender, FileSender)
        assert isinstance(c.sheets, SheetsPublisher)
        assert c.data_source._fallback is not None  # Twelve Data enabled by its key
        assert not c.dry_run
        assert (tmp_path / DB_NAME).is_file()
    finally:
        c.close()


def test_build_container_rejects_csv_without_offline(tmp_path: Path):
    from uncorrelated_trading.config import AppConfig
    from uncorrelated_trading.errors import ConfigError

    with pytest.raises(ConfigError):
        app.build_container(_settings(tmp_path), AppConfig.load(OFFLINE_TOML), csv_path=CSV)


def test_make_sender_backends(tmp_path: Path):
    from uncorrelated_trading.clock import SystemClock

    clock = SystemClock()
    smtp = _settings(
        tmp_path, EMAIL_BACKEND="smtp", SMTP_USERNAME="bot@example.com", SMTP_PASSWORD="pw"
    )
    resend = _settings(tmp_path, EMAIL_BACKEND="resend", RESEND_API_KEY="re_fake")
    assert app.make_sender(smtp, clock).name == "smtp"
    assert app.make_sender(resend, clock).name == "resend"
    assert app.make_sender(_settings(tmp_path), clock).name == "file"
