import email
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import Attachment, EmailMessage
from uncorrelated_trading.report.email.file_sender import FileSender

NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


def test_file_sender_writes_eml_and_attachment(tmp_path: Path):
    outbox = tmp_path / "data" / "outbox"
    message = EmailMessage(
        sender="bot@example.com",
        recipients=("a@example.com",),
        subject="Daily brief",
        text_body="plain body",
        html_body="<p>html body</p>",
        attachments=(Attachment("report.xlsx", b"PK\x03\x04fake", "application/octet-stream"),),
        idempotency_key="run1-abc123",
    )
    sender = FileSender(outbox, FakeClock(NOW))
    receipt = sender.send(message)
    eml = outbox / "run1-abc123.eml"
    assert eml.is_file()
    assert receipt.message_id == str(eml)
    assert receipt.backend == "file"
    assert receipt.sent_at == NOW
    parsed = email.message_from_bytes(eml.read_bytes())
    assert parsed["Subject"] == "Daily brief"
    assert [p.get_filename() for p in parsed.walk() if p.get_filename()] == ["report.xlsx"]
    assert (outbox / "run1-abc123" / "report.xlsx").read_bytes() == b"PK\x03\x04fake"
    assert sender.name == "file"


@pytest.mark.parametrize("bad", ["../evil.xlsx", "sub/evil.xlsx", "..", ""])
def test_file_sender_rejects_unsafe_attachment_name(tmp_path: Path, bad: str):
    message = EmailMessage(
        sender="bot@example.com",
        recipients=("a@example.com",),
        subject="s",
        text_body="t",
        html_body="<p>h</p>",
        attachments=(Attachment(bad, b"x", "application/octet-stream"),),
        idempotency_key="run1-abc123",
    )
    with pytest.raises(EmailSendError) as exc_info:
        FileSender(tmp_path / "outbox", FakeClock(NOW)).send(message)
    assert exc_info.value.retryable is False
    assert not (tmp_path / "evil.xlsx").exists()
