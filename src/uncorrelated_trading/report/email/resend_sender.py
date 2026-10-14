"""Resend (https://resend.com) HTTP email backend.

Posts JSON to ``/emails`` with an ``Idempotency-Key`` equal to the message's
idempotency key, so a retried send never produces a duplicate email.
"""

from __future__ import annotations

import base64
from typing import Any

import requests

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import EmailMessage, SendReceipt

RESEND_URL = "https://api.resend.com/emails"
RETRY_SLEEP_S = 2.0
_BACKEND = "resend"
_MAX_ERROR_TEXT = 500


def _payload(message: EmailMessage) -> dict[str, Any]:
    """Build the Resend JSON body for ``message``."""
    return {
        "from": message.sender,
        "to": list(message.recipients),
        "subject": message.subject,
        "text": message.text_body,
        "html": message.html_body,
        "attachments": [
            {
                "filename": a.filename,
                "content": base64.b64encode(a.content).decode("ascii"),
                "content_type": a.mime_type,
            }
            for a in message.attachments
        ],
    }


class ResendSender:
    """Deliver messages through the Resend REST API.

    Parameters
    ----------
    api_key : str
        Resend API key (never logged or included in error messages).
    clock : Clock
        Receipt timestamps and the back-off sleep before the single retry.
    session : requests.Session, optional
        HTTP session; a new one is created when omitted (tests inject a fake).
    timeout_s : float, default 30.0
        Per-request timeout.
    """

    name = "resend"

    def __init__(
        self,
        api_key: str,
        clock: Clock,
        session: requests.Session | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._clock = clock
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        """Return a representation that never includes the API key."""
        return f"ResendSender(timeout_s={self._timeout_s})"

    def _post(self, message: EmailMessage, body: dict[str, Any]) -> requests.Response:
        """POST once, mapping transport failures to a retryable error."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": message.idempotency_key,
        }
        try:
            return self._session.post(
                RESEND_URL, headers=headers, json=body, timeout=self._timeout_s
            )
        except requests.RequestException as exc:
            raise EmailSendError(
                _BACKEND, f"request failed: {type(exc).__name__}", retryable=True
            ) from exc

    def send(self, message: EmailMessage) -> SendReceipt:
        """Send ``message`` via Resend.

        Parameters
        ----------
        message : EmailMessage
            The message to deliver.

        Returns
        -------
        SendReceipt
            Receipt whose ``message_id`` is the Resend email id.

        Raises
        ------
        EmailSendError
            ``retryable=True`` for 429/5xx (after one internal retry) and transport errors;
            ``retryable=False`` for 401/403/422 and other 4xx responses.
        """
        body = _payload(message)
        response = self._post(message, body)
        if response.status_code == 429 or response.status_code >= 500:
            self._clock.sleep(RETRY_SLEEP_S)
            response = self._post(message, body)
        status = response.status_code
        text = response.text[:_MAX_ERROR_TEXT]
        if status == 429 or status >= 500:
            raise EmailSendError(_BACKEND, f"HTTP {status}: {text}", retryable=True)
        if not 200 <= status < 300:
            raise EmailSendError(_BACKEND, f"HTTP {status}: {text}", retryable=False)
        try:
            message_id = str(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise EmailSendError(
                _BACKEND, f"unexpected success response: {text}", retryable=False
            ) from exc
        return SendReceipt(backend=self.name, message_id=message_id, sent_at=self._clock.now())
