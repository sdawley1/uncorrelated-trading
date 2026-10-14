"""SMTP email backend built on the standard library ``smtplib``.

Uses STARTTLS (typically port 587) or implicit TLS via ``SMTP_SSL`` (typically 465).
Failures are mapped to :class:`EmailSendError` with ``retryable`` set so a cron retry
can decide whether trying again makes sense (e.g. Gmail 535 is permanent).
"""

from __future__ import annotations

import contextlib
import smtplib
from collections.abc import Callable
from email.utils import make_msgid

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import EmailMessage, SendReceipt
from uncorrelated_trading.report.email.base import build_mime

_BACKEND = "smtp"


def _is_transient(code: int) -> bool:
    """Return True for SMTP 4xx (transient) reply codes such as 421, 450, 451, 452."""
    return 400 <= code < 500


def _map_error(exc: Exception) -> EmailSendError:
    """Translate an ``smtplib``/socket exception into an :class:`EmailSendError`."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailSendError(_BACKEND, f"authentication failed ({exc.smtp_code})", retryable=False)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return EmailSendError(_BACKEND, f"server disconnected: {exc}", retryable=True)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        retryable = bool(codes) and all(_is_transient(c) for c in codes)
        return EmailSendError(_BACKEND, f"recipients refused: {codes}", retryable=retryable)
    if isinstance(exc, smtplib.SMTPResponseException):
        return EmailSendError(
            _BACKEND,
            f"server replied {exc.smtp_code}: {exc.smtp_error!r}",
            retryable=_is_transient(exc.smtp_code),
        )
    if isinstance(exc, smtplib.SMTPException):
        return EmailSendError(_BACKEND, f"smtp error: {exc}", retryable=False)
    # TimeoutError (== socket.timeout), connection refused, DNS failures, ...
    return EmailSendError(_BACKEND, f"network error: {exc!r}", retryable=True)


class SmtpSender:
    """Deliver messages over SMTP.

    Parameters
    ----------
    host, port : str, int
        SMTP server address.
    username, password : str or None
        Credentials; ``login`` is called only when both are set.
    use_ssl : bool
        True for implicit TLS (``SMTP_SSL``), False for plain connect + ``starttls``.
    clock : Clock
        Source of the receipt timestamp.
    timeout_s : float, default 30.0
        Socket timeout.
    smtp_factory : callable, optional
        Replaces ``smtplib.SMTP``/``smtplib.SMTP_SSL`` (used by tests).
    """

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_ssl: bool,
        clock: Clock,
        timeout_s: float = 30.0,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._clock = clock
        self._timeout_s = timeout_s
        self._smtp_factory = smtp_factory

    def __repr__(self) -> str:
        """Return a representation that never includes the password."""
        return (
            f"SmtpSender(host={self._host!r}, port={self._port}, "
            f"username={self._username!r}, use_ssl={self._use_ssl})"
        )

    def send(self, message: EmailMessage) -> SendReceipt:
        """Send ``message`` over SMTP.

        Parameters
        ----------
        message : EmailMessage
            The message to deliver.

        Returns
        -------
        SendReceipt
            Receipt whose ``message_id`` is the generated ``Message-ID`` header.

        Raises
        ------
        EmailSendError
            ``retryable=False`` for authentication (535) and permanent 5xx failures;
            ``retryable=True`` for disconnects, timeouts, network errors and 4xx replies.
        """
        mime = build_mime(message)
        _, _, domain = message.sender.rpartition("@")
        message_id = make_msgid(domain=domain or None)
        mime["Message-ID"] = message_id
        factory: Callable[..., smtplib.SMTP]
        if self._smtp_factory is not None:
            factory = self._smtp_factory
        elif self._use_ssl:
            factory = smtplib.SMTP_SSL
        else:
            factory = smtplib.SMTP
        try:
            smtp = factory(self._host, self._port, timeout=self._timeout_s)
        except (smtplib.SMTPException, OSError) as exc:
            raise _map_error(exc) from exc
        try:
            if not self._use_ssl:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(mime)
        except (smtplib.SMTPException, OSError) as exc:
            raise _map_error(exc) from exc
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                smtp.quit()
        return SendReceipt(backend=self.name, message_id=message_id, sent_at=self._clock.now())
