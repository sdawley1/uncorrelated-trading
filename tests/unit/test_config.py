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


def test_sheets_enabled_needs_both_vars(tmp_path):
    one = Settings.from_env(env={**GOOD_ENV, "GOOGLE_SHEET_ID": "sheet"}, dotenv_path=None)
    assert one.sheets_enabled is False
    key_file = tmp_path / "sa.json"
    key_file.write_text("{}", encoding="utf-8")  # must exist since the final fix wave (L2)
    both = Settings.from_env(
        env={**GOOD_ENV, "GOOGLE_SHEET_ID": "sheet", "GOOGLE_SERVICE_ACCOUNT_FILE": str(key_file)},
        dotenv_path=None,
    )
    assert both.sheets_enabled is True
    assert both.google_service_account_file == key_file


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


# ----------------------------------------------------------------------------- packaged config

REPO = Path(__file__).resolve().parents[2]


def test_packaged_default_config_is_identical_to_repo_copy():
    from importlib import resources

    packaged = resources.files("uncorrelated_trading") / "config_default.toml"
    assert packaged.read_bytes() == (REPO / "config" / "default.toml").read_bytes()


def test_config_falls_back_to_packaged_default(tmp_path, monkeypatch):
    from uncorrelated_trading.config import packaged_config_path

    monkeypatch.chdir(tmp_path)  # no ./config/default.toml here
    s = Settings.from_env(env=GOOD_ENV, dotenv_path=None)
    assert s.config_file == packaged_config_path()
    assert s.config_file.is_file()
    assert len(AppConfig.load(s.config_file).universe.symbols) > 0


def test_local_config_preferred_over_packaged(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    s = Settings.from_env(env=GOOD_ENV, dotenv_path=None)
    assert s.config_file == Path("config/default.toml")
    explicit = Settings.from_env(env={**GOOD_ENV, "UT_CONFIG_FILE": "x.toml"}, dotenv_path=None)
    assert explicit.config_file == Path("x.toml")


# ----------------------------------------------------------------------------- placeholders


def test_env_example_copied_verbatim_is_rejected(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes((REPO / ".env.example").read_bytes())
    with pytest.raises(ConfigError) as ei:
        Settings.from_env(env={}, dotenv_path=dotenv)
    msg = str(ei.value)
    for key in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "TWELVEDATA_API_KEY",
        "SMTP_PASSWORD",
        "GOOGLE_SHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
    ):
        assert f"{key} looks like a placeholder from .env.example" in msg


@pytest.mark.parametrize(
    "key, value",
    [
        ("ALPACA_API_KEY", "XXXXXXXX"),
        ("ALPACA_SECRET_KEY", "xxxxxxxxxxxxxxxx"),
        ("ALPACA_API_KEY", "PKXXXXXXXXXXXXXXXXXX"),
        ("TWELVEDATA_API_KEY", "XXXXXXXXXXXXXXXX"),
        ("SMTP_PASSWORD", "xxxxxxxxxxxxxxxx"),
        ("GOOGLE_SERVICE_ACCOUNT_FILE", "/path/to/service-account.json"),
    ],
)
def test_placeholder_values_rejected(key, value):
    with pytest.raises(ConfigError, match=f"{key} looks like a placeholder from .env.example"):
        Settings.from_env(env={**GOOD_ENV, key: value}, dotenv_path=None)


@pytest.mark.parametrize("value", ["XXXXXXX", "PKAB12XXXXXXXXXXCD", "XXXXXXXXa", "PKXXXXxxxxXXXX"])
def test_non_placeholder_values_accepted(value):
    s = Settings.from_env(env={**GOOD_ENV, "ALPACA_API_KEY": value}, dotenv_path=None)
    assert s.alpaca_api_key == value


def test_missing_service_account_file_rejected(tmp_path):
    env = {
        **GOOD_ENV,
        "GOOGLE_SHEET_ID": "sheet",
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "missing.json"),
    }
    with pytest.raises(ConfigError, match="GOOGLE_SERVICE_ACCOUNT_FILE does not exist"):
        Settings.from_env(env=env, dotenv_path=None)


def test_ci_uses_matrix_python_and_license_present():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "UV_PYTHON: ${{ matrix.python }}" in ci
    license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert "Copyright (c) 2026 The uncorrelated-trading authors" in license_text


def test_env_example_leaves_config_file_unset():
    # Setting UT_CONFIG_FILE would defeat the packaged-default fallback outside the repo.
    from dotenv import dotenv_values

    assert "UT_CONFIG_FILE" not in dotenv_values(REPO / ".env.example")
