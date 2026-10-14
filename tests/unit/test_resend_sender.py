import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from uncorrelated_trading.clock import FakeClock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import Attachment, EmailMessage
from uncorrelated_trading.report.email.resend_sender import ResendSender

NOW = datetime(2026, 10, 13, 21, 30, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "resend"
FAKE_KEY = "re_FAKEKEYXXXXXXXXXXXXXXXX"


def make_message() -> EmailMessage:
    return EmailMessage(
        sender="bot@example.com",
        recipients=("a@example.com",),
        subject="Daily brief",
        text_body="plain body",
        html_body="<p>html body</p>",
        attachments=(Attachment("report.xlsx", b"\x00\x01binary", "application/octet-stream"),),
        idempotency_key="run1-abc123",
    )


class FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body

    def json(self) -> Any:
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict[str, str], json: Any, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.responses.pop(0)


def ok() -> FakeResponse:
    return FakeResponse(200, (FIXTURES / "send_ok.json").read_text(encoding="utf-8"))


def rate_limited() -> FakeResponse:
    return FakeResponse(429, (FIXTURES / "send_429.json").read_text(encoding="utf-8"))


def test_resend_posts_expected_json():
    clock = FakeClock(NOW)
    session = FakeSession([ok()])
    sender = ResendSender(FAKE_KEY, clock, session=session)  # type: ignore[arg-type]
    receipt = sender.send(make_message())
    (call,) = session.calls
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert call["headers"]["Idempotency-Key"] == "run1-abc123"
    assert call["timeout"] == 30.0
    body = call["json"]
    assert body["from"] == "bot@example.com"
    assert body["to"] == ["a@example.com"]
    assert body["subject"] == "Daily brief"
    assert body["text"] == "plain body"
    assert body["html"] == "<p>html body</p>"
    att = body["attachments"][0]
    assert att["filename"] == "report.xlsx"
    assert att["content"] == base64.b64encode(b"\x00\x01binary").decode("ascii")
    assert att["content_type"] == "application/octet-stream"
    assert receipt.backend == "resend"
    assert receipt.message_id == "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
    assert receipt.sent_at == NOW
    assert clock.sleep_calls == []


def test_resend_429_retryable():
    clock = FakeClock(NOW)
    session = FakeSession([rate_limited(), rate_limited()])
    sender = ResendSender(FAKE_KEY, clock, session=session)  # type: ignore[arg-type]
    with pytest.raises(EmailSendError) as exc_info:
        sender.send(make_message())
    assert exc_info.value.retryable is True
    assert exc_info.value.backend == "resend"
    assert len(session.calls) == 2
    assert clock.sleep_calls == [2.0]


def test_resend_5xx_then_ok_retries_once():
    clock = FakeClock(NOW)
    session = FakeSession([FakeResponse(503, "unavailable"), ok()])
    sender = ResendSender(FAKE_KEY, clock, session=session)  # type: ignore[arg-type]
    receipt = sender.send(make_message())
    assert receipt.message_id == "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
    assert len(session.calls) == 2
    assert session.calls[1]["headers"]["Idempotency-Key"] == "run1-abc123"
    assert clock.sleep_calls == [2.0]


@pytest.mark.parametrize("status", [401, 403, 422])
def test_resend_401_not_retryable(status: int):
    clock = FakeClock(NOW)
    body = json.dumps({"statusCode": status, "message": "API key is invalid", "name": "x"})
    session = FakeSession([FakeResponse(status, body)])
    sender = ResendSender(FAKE_KEY, clock, session=session)  # type: ignore[arg-type]
    with pytest.raises(EmailSendError) as exc_info:
        sender.send(make_message())
    assert exc_info.value.retryable is False
    assert "API key is invalid" in str(exc_info.value)
    assert FAKE_KEY not in str(exc_info.value)
    assert len(session.calls) == 1
    assert clock.sleep_calls == []


class BrokenSession:
    def post(self, url: str, headers: dict[str, str], json: Any, timeout: float) -> FakeResponse:
        raise requests.ConnectionError("boom")


def test_resend_transport_error_retryable():
    sender = ResendSender(FAKE_KEY, FakeClock(NOW), session=BrokenSession())  # type: ignore[arg-type]
    with pytest.raises(EmailSendError) as exc_info:
        sender.send(make_message())
    assert exc_info.value.retryable is True


def test_resend_repr_hides_key():
    assert FAKE_KEY not in repr(ResendSender(FAKE_KEY, FakeClock(NOW), session=FakeSession([])))  # type: ignore[arg-type]
