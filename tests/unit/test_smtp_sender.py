import email
import smtplib
import socket
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage

import pytest

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import Attachment, EmailMessage
from uncorrelated_trading.report.email.base import build_mime
from uncorrelated_trading.report.email.smtp_sender import SmtpSender

NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


def make_message() -> EmailMessage:
    return EmailMessage(
        sender="bot@example.com",
        recipients=("a@example.com", "b@example.com"),
        subject="Daily brief",
        text_body="plain body",
        html_body="<p>html body</p>",
        attachments=(
            Attachment(
                "report.xlsx",
                b"PK\x03\x04fake",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ),
        idempotency_key="run1-abc123",
    )


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float = 0.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.sent: list[MimeMessage] = []
        self.login_args: tuple[str, str] | None = None
        FakeSMTP.instances.append(self)

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, msg: MimeMessage) -> dict[str, object]:
        self.calls.append("send_message")
        self.sent.append(msg)
        return {}

    def quit(self) -> None:
        self.calls.append("quit")


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeSMTP.instances.clear()


def test_build_mime_has_text_html_and_attachment():
    mime = build_mime(make_message())
    parsed = email.message_from_bytes(bytes(mime))
    assert parsed["From"] == "bot@example.com"
    assert parsed["To"] == "a@example.com, b@example.com"
    assert parsed["Subject"] == "Daily brief"
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types
    assert "text/html" in types
    attachments = [p for p in parsed.walk() if p.get_filename()]
    assert len(attachments) == 1
    att = attachments[0]
    assert att.get_filename() == "report.xlsx"
    assert att.get_content_type() == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert att.get_payload(decode=True) == b"PK\x03\x04fake"


def test_smtp_sends_via_starttls():
    clock = FakeClock(NOW)
    sender = SmtpSender(
        "smtp.example.com", 587, "user", "secret", False, clock, smtp_factory=FakeSMTP
    )
    receipt = sender.send(make_message())
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 587, 30.0)
    assert smtp.calls == ["starttls", "login", "send_message", "quit"]
    assert smtp.login_args == ("user", "secret")
    assert receipt.backend == "smtp"
    assert receipt.sent_at == NOW
    assert receipt.message_id
    assert smtp.sent[0]["Message-ID"] == receipt.message_id
    assert sender.name == "smtp"


def test_smtp_ssl_skips_starttls_and_login_without_credentials():
    sender = SmtpSender(
        "smtp.example.com", 465, None, None, True, FakeClock(NOW), smtp_factory=FakeSMTP
    )
    sender.send(make_message())
    (smtp,) = FakeSMTP.instances
    assert smtp.calls == ["send_message", "quit"]


class AuthFailSMTP(FakeSMTP):
    def login(self, user: str, password: str) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")


class DisconnectSMTP(FakeSMTP):
    def send_message(self, msg: MimeMessage) -> dict[str, object]:
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


class TimeoutSMTP(FakeSMTP):
    def starttls(self) -> None:
        raise TimeoutError("timed out")


class Transient451SMTP(FakeSMTP):
    def send_message(self, msg: MimeMessage) -> dict[str, object]:
        raise smtplib.SMTPDataError(451, b"try again later")


class Permanent550SMTP(FakeSMTP):
    def send_message(self, msg: MimeMessage) -> dict[str, object]:
        raise smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such user")})


def _send(factory: type[FakeSMTP]) -> EmailSendError:
    sender = SmtpSender("h", 587, "u", "p", False, FakeClock(NOW), smtp_factory=factory)
    with pytest.raises(EmailSendError) as exc_info:
        sender.send(make_message())
    return exc_info.value


def test_smtp_auth_error_not_retryable():
    err = _send(AuthFailSMTP)
    assert err.retryable is False
    assert err.backend == "smtp"
    assert "535" in str(err)
    assert FakeSMTP.instances[0].calls[-1] == "quit"


def test_smtp_disconnect_retryable():
    assert _send(DisconnectSMTP).retryable is True


def test_smtp_timeout_retryable():
    assert _send(TimeoutSMTP).retryable is True
    assert issubclass(socket.timeout, OSError)


def test_smtp_transient_4xx_retryable():
    assert _send(Transient451SMTP).retryable is True


def test_smtp_permanent_5xx_not_retryable():
    assert _send(Permanent550SMTP).retryable is False
