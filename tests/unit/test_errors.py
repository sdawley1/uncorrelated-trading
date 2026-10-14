from uncorrelated_trading.errors import (
    ConfigError,
    DataSourceError,
    EmailSendError,
    RateLimitError,
    UncorrelatedTradingError,
)


def test_config_error_lists_all_problems():
    err = ConfigError(["ALPACA_API_KEY missing", "EMAIL_TO missing"])
    assert "ALPACA_API_KEY missing" in str(err) and "EMAIL_TO missing" in str(err)
    assert err.problems == ["ALPACA_API_KEY missing", "EMAIL_TO missing"]


def test_rate_limit_is_retryable():
    assert RateLimitError("alpaca").retryable is True


def test_rate_limit_is_a_data_source_error():
    err = RateLimitError("twelvedata")
    assert isinstance(err, DataSourceError)
    assert isinstance(err, UncorrelatedTradingError)
    assert err.provider == "twelvedata"
    assert str(err) == "twelvedata: rate limit exceeded"


def test_data_source_error_carries_provider_and_retryable():
    err = DataSourceError("alpaca", "401 unauthorized", retryable=False)
    assert err.provider == "alpaca"
    assert err.retryable is False
    assert "401 unauthorized" in str(err)


def test_email_send_error_carries_backend_and_retryable():
    err = EmailSendError("resend", "429 too many requests", retryable=True)
    assert err.backend == "resend"
    assert err.retryable is True
    assert str(err) == "resend: 429 too many requests"
