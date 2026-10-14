# `uncorrelated_trading.config`

Two separate configuration sources.

## `Settings` (environment and `.env`)

`Settings.from_env(env=None, dotenv_path=<cwd>/.env, *, require_alpaca=True, require_email=True)`

- `env=None` reads `os.environ`. Values from `env` always win over the `.env` file.
- Omitting `dotenv_path` loads `./.env` if it exists; passing `None` disables file loading.
- Blank values count as unset.
- All missing or malformed variables are reported together in a single `ConfigError`.
- Required: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `EMAIL_BACKEND` (`smtp` | `resend` | `file`),
  `EMAIL_FROM`, `EMAIL_TO` (comma-separated). `smtp` also requires `SMTP_USERNAME` and
  `SMTP_PASSWORD`. `resend` requires `RESEND_API_KEY`.
- Defaults: `ALPACA_PAPER=true`, `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
  `SMTP_USE_SSL=false`, `UT_DATA_DIR=data`, `UT_CONFIG_FILE=config/default.toml`,
  `UT_TIMEZONE=America/New_York` (must be a valid IANA zone), `LOG_LEVEL=INFO`.
- Booleans accept `1/true/yes/on` and `0/false/no/off` in any case. Any other value is an error.
- Offline use (`--offline --dry-run`): `require_alpaca=False` turns missing Alpaca keys into `""`.
  `require_email=False` makes a missing backend `"file"`, the sender `""` and the recipients `()`.
  Malformed values are still errors.
- `sheets_enabled` is True when both `GOOGLE_SERVICE_ACCOUNT_FILE` and `GOOGLE_SHEET_ID` are set.
  `db_path` is `data_dir / "uncorrelated_trading.sqlite3"`.
- `repr()` shows `***` for the Alpaca, Twelve Data, SMTP-password and Resend keys.

## `AppConfig` (TOML)

`AppConfig.load(path)` parses the file with `tomllib` into the sections `universe`, `data`,
`analysis` (an `AnalysisParams` from `models.py`), `schedule` and `report`.

- If a section or key is missing, the spec A5 default is used, so a partial file is valid.
  The defaults in code match `config/default.toml`.
- The following are errors, and every problem is reported in one `ConfigError`:
  - unknown sections or keys
  - wrong types (a bool is not accepted as a number)
  - out-of-range values
- Universe rules:
  - symbols are unique and upper-case;
  - there are 2 to 200 of them;
  - none matches an `excluded_patterns` entry (fnmatch, case-sensitive).
- Analysis rules:
  - `tau` and `tau_spearman` are in (0, 1);
  - `primary_window` is one of `windows`;
  - `momentum_long > momentum_skip >= 1`;
  - `momentum_weights` has 3 non-negative entries.
- Schedule rules: times are `HH:MM`, and `send_on_days` is one of `weekdays`, `trading_days`,
  `every_day`.
- `analysis.min_history_fraction` appears in the spec TOML. It is accepted and checked to be in
  [0, 1], but `AnalysisParams` has no field for it, so its value is dropped.
