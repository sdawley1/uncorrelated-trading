"""Backend-neutral email sender protocol and MIME construction.

``build_mime`` is shared by the SMTP and file senders so that what is written to the
dry-run outbox is byte-for-byte the message SMTP would transmit (minus the Message-ID).
"""

from __future__ import annotations

import email.message
from typing import Protocol

from uncorrelated_trading.models import EmailMessage, SendReceipt


class EmailSender(Protocol):
    """Anything that can deliver an :class:`EmailMessage` and return a receipt."""

    name: str

    def send(self, message: EmailMessage) -> SendReceipt:
        """Deliver ``message``.

        Parameters
        ----------
        message : EmailMessage
            The message to deliver.

        Returns
        -------
        SendReceipt
            Backend name, backend message id and the send instant.

        Raises
        ------
        EmailSendError
            If delivery failed; ``retryable`` says whether a later retry may succeed.
        """
        ...


def build_mime(message: EmailMessage) -> email.message.EmailMessage:
    """Build a multipart MIME message (text + HTML alternative + attachments).

    Parameters
    ----------
    message : EmailMessage
        Backend-neutral message.

    Returns
    -------
    email.message.EmailMessage
        ``multipart/mixed`` (when attachments exist) wrapping a
        ``multipart/alternative`` with ``text/plain`` and ``text/html`` parts.
    """
    mime = email.message.EmailMessage()
    mime["From"] = message.sender
    mime["To"] = ", ".join(message.recipients)
    mime["Subject"] = message.subject
    mime.set_content(message.text_body)
    mime.add_alternative(message.html_body, subtype="html")
    for attachment in message.attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        mime.add_attachment(
            attachment.content, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    return mime
