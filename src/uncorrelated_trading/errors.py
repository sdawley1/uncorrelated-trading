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
