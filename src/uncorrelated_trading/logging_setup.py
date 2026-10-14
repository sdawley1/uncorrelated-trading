"""Logging configuration (human or JSON lines) with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Mapping
from typing import Any

_PACKAGE_LOGGER = "uncorrelated_trading"
_MASK = "***"


class _RedactingFormatter(logging.Formatter):
    """Text formatter that also masks secrets in formatted tracebacks."""

    def formatException(self, ei: Any) -> str:  # noqa: N802 (logging API name)
        """Format the exception and mask every registered secret."""
        return _REDACTION.redact(super().formatException(ei))


class _JsonFormatter(_RedactingFormatter):
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


class _RedactionFilter(logging.Filter):
    """Replace every registered secret with ``***`` in ``record.msg`` and string args."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()

    def redact(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced by ``***``."""
        return self._redact(text) if self.secrets else text

    def _redact(self, text: str) -> str:
        # Longest first so a secret that contains another is masked whole.
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, _MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {
                k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        elif isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) if isinstance(a, str) else a for a in record.args)
        return True


# One shared filter: secrets registered before or after configure_logging() are both honoured.
_REDACTION = _RedactionFilter()


def _install_redaction(logger: logging.Logger) -> None:
    # Filters on a logger do not see records propagated from child loggers, so the filter is
    # attached to every handler of the package logger as well as to the logger itself.
    for target in (logger, *logger.handlers):
        if _REDACTION not in target.filters:
            target.addFilter(_REDACTION)


def configure_logging(level: str, json_format: bool = False) -> None:
    """Configure the ``uncorrelated_trading`` logger hierarchy.

    Parameters
    ----------
    level : str
        Logging level name, e.g. ``"INFO"``.
    json_format : bool
        Emit one JSON object per line instead of human-readable text.
    """
    logger = logging.getLogger(_PACKAGE_LOGGER)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    fmt: logging.Formatter = (
        _JsonFormatter()
        if json_format
        else _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    _install_redaction(logger)


def add_redaction(secrets: Iterable[str]) -> None:
    """Mask the given secret values as ``***`` in all package log output.

    Installs a ``logging.Filter`` on the ``uncorrelated_trading`` logger and its handlers that
    replaces each non-empty secret in ``record.msg`` and in string ``record.args``; the
    package formatters also mask secrets in formatted exception tracebacks. Secrets
    accumulate across calls and survive later calls to :func:`configure_logging`.

    Parameters
    ----------
    secrets : Iterable[str]
        Secret values (API keys, passwords). Empty strings are ignored.
    """
    _REDACTION.secrets.update(s for s in secrets if s)
    _install_redaction(logging.getLogger(_PACKAGE_LOGGER))
