from pathlib import Path

import pytest

from uncorrelated_trading.config import AppConfig, Settings
from uncorrelated_trading.errors import ConfigError

GOOD_ENV = {
    "ALPACA_API_KEY": "PKTEST",
    "ALPACA_SECRET_KEY": "SKTEST",
    "EMAIL_BACKEND": "smtp",
    "EMAIL_FROM": "me@example.com",
    "EMAIL_TO": "a@example.com, b@example.com",
    "SMTP_USERNAME": "me@example.com",
    "SMTP_PASSWORD": "apppass",
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


@pytest.mark.parametrize(
    "bad, expect",
    [
        ('[universe]\nsymbols=["XOM","XOM"]\n', "duplicate"),
        ('[universe]\nsymbols=["USO"]\nexcluded_patterns=["USO"]\n', "excluded"),
        ("[analysis]\ntau=1.5\n", "tau"),
    ],
)
def test_invalid_toml_rejected(tmp_path, bad, expect):
    p = tmp_path / "c.toml"
    p.write_text(bad)
    with pytest.raises(ConfigError, match=expect):
        AppConfig.load(p)


# ---------------------------------------------------------------------------
# Additional behaviour beyond the brief's pinned cases
# ---------------------------------------------------------------------------


def test_dotenv_fills_missing_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ALPACA_API_KEY=FROMFILE\nTWELVEDATA_API_KEY=TDFAKE\n")
    env = {k: v for k, v in GOOD_ENV.items() if k != "ALPACA_API_KEY"}
    s = Settings.from_env(env=env, dotenv_path=p)
    assert s.alpaca_api_key == "FROMFILE"
    assert s.twelvedata_api_key == "TDFAKE"


def test_default_dotenv_path_is_cwd(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALPACA_API_KEY=CWDFILE\n")
    monkeypatch.chdir(tmp_path)
    env = {k: v for k, v in GOOD_ENV.items() if k != "ALPACA_API_KEY"}
    assert Settings.from_env(env=env).alpaca_api_key == "CWDFILE"
    with pytest.raises(ConfigError, match="ALPACA_API_KEY"):
        Settings.from_env(env=env, dotenv_path=None)


def test_env_none_uses_os_environ(monkeypatch):
    for k, v in GOOD_ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env(dotenv_path=None)
    assert s.alpaca_api_key == "PKTEST"


def test_defaults_and_optional_values():
    s = Settings.from_env(env=GOOD_ENV, dotenv_path=None)
    assert s.twelvedata_api_key is None
    assert s.smtp_use_ssl is False
    assert s.data_dir == Path("data")
    assert s.config_file == Path("config/default.toml")
    assert s.timezone == "America/New_York"
    assert s.log_level == "INFO"
    assert s.sheets_enabled is False


def test_sheets_enabled_needs_both_vars():
    one = Settings.from_env(env={**GOOD_ENV, "GOOGLE_SHEET_ID": "sheet"}, dotenv_path=None)
    assert one.sheets_enabled is False
    both = Settings.from_env(
        env={**GOOD_ENV, "GOOGLE_SHEET_ID": "sheet", "GOOGLE_SERVICE_ACCOUNT_FILE": "sa.json"},
        dotenv_path=None,
    )
    assert both.sheets_enabled is True
    assert both.google_service_account_file == Path("sa.json")


@pytest.mark.parametrize(
    "raw, expected",
    [("1", True), ("TRUE", True), ("yes", True), ("On", True), ("false", False), ("0", False)],
)
def test_alpaca_paper_bool_parsing(raw, expected):
    s = Settings.from_env(env={**GOOD_ENV, "ALPACA_PAPER": raw}, dotenv_path=None)
    assert s.alpaca_paper is expected


def test_malformed_values_reported_together():
    env = {
        **GOOD_ENV,
        "ALPACA_PAPER": "maybe",
        "SMTP_PORT": "abc",
        "EMAIL_BACKEND": "pigeon",
        "UT_TIMEZONE": "Mars/Olympus",
    }
    with pytest.raises(ConfigError) as ei:
        Settings.from_env(env=env, dotenv_path=None)
    msg = str(ei.value)
    for key in ("ALPACA_PAPER", "SMTP_PORT", "EMAIL_BACKEND", "UT_TIMEZONE"):
        assert key in msg


def test_resend_backend_ok_without_smtp_password():
    env = {k: v for k, v in GOOD_ENV.items() if not k.startswith("SMTP_")}
    s = Settings.from_env(
        env={**env, "EMAIL_BACKEND": "resend", "RESEND_API_KEY": "re_FAKE"}, dotenv_path=None
    )
    assert s.email_backend == "resend" and s.resend_api_key == "re_FAKE"
    assert "re_FAKE" not in repr(s)


def test_repr_masks_all_secret_fields():
    s = Settings.from_env(
        env={**GOOD_ENV, "TWELVEDATA_API_KEY": "TDSECRET", "RESEND_API_KEY": "RESECRET"},
        dotenv_path=None,
    )
    r = repr(s)
    for secret in ("PKTEST", "SKTEST", "apppass", "TDSECRET", "RESECRET"):
        assert secret not in r
    assert "me@example.com" in r


def test_offline_mode_defaults():
    s = Settings.from_env(env={}, dotenv_path=None, require_alpaca=False, require_email=False)
    assert s.alpaca_secret_key == ""
    assert s.email_from == "" and s.email_to == ()
    assert s.smtp_password is None


def test_partial_toml_uses_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[report]\ntop_pairs = 3\n")
    cfg = AppConfig.load(p)
    default = AppConfig.load(Path("config/default.toml"))
    assert cfg.report.top_pairs == 3
    assert cfg.universe == default.universe
    assert cfg.analysis == default.analysis
    assert cfg.data == default.data
    assert cfg.schedule == default.schedule


def test_default_toml_values():
    cfg = AppConfig.load(Path("config/default.toml"))
    assert cfg.analysis.windows == (60, 120, 250)
    assert cfg.analysis.momentum_weights == (0.4, 0.3, 0.3)
    assert cfg.analysis.tau == pytest.approx(0.20)
    assert cfg.schedule.refresh_time.hour == 16 and cfg.schedule.refresh_time.minute == 25
    assert cfg.schedule.send_on_days == "weekdays"
    assert cfg.data.alpaca_feed == "sip"
    assert cfg.report.subject_prefix == "[uncorrelated-trading]"
    assert "USO" in cfg.universe.excluded_patterns


@pytest.mark.parametrize(
    "bad, expect",
    [
        ('[universe]\nsymbols=["xom","CVX"]\n', "upper-case"),
        ('[universe]\nsymbols=["XOM"]\n', "2 to 200"),
        ("[analysis]\nprimary_window=30\n", "primary_window"),
        ("[analysis]\nmomentum_long=10\nmomentum_skip=20\n", "momentum_long"),
        ("[analysis]\nmomentum_skip=0\n", "momentum_skip"),
        ("[analysis]\nmomentum_weights=[0.5,0.5]\n", "momentum_weights"),
        ("[analysis]\nmomentum_weights=[0.5,-0.1,0.6]\n", "momentum_weights"),
        ('[schedule]\nsend_time="7am"\n', "send_time"),
        ('[schedule]\nrefresh_time="25:00"\n', "refresh_time"),
        ('[schedule]\nsend_on_days="sundays"\n', "send_on_days"),
        ("[analysis]\ntua=0.3\n", "tua"),
        ("[analysys]\ntau=0.3\n", "analysys"),
        ('[data]\nbackfill_calendar_days="lots"\n', "backfill_calendar_days"),
        ("[analysis\n", "TOML"),
    ],
)
def test_more_invalid_toml_rejected(tmp_path, bad, expect):
    p = tmp_path / "c.toml"
    p.write_text(bad)
    with pytest.raises(ConfigError, match=expect):
        AppConfig.load(p)


def test_toml_problems_collected_at_once(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[analysis]\ntau=2.0\nbogus=1\n[schedule]\nsend_time="x"\n')
    with pytest.raises(ConfigError) as ei:
        AppConfig.load(p)
    assert len(ei.value.problems) >= 3


def test_missing_toml_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        AppConfig.load(tmp_path / "nope.toml")
