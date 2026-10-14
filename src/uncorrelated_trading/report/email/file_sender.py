"""File-system email backend for ``--dry-run``, end-to-end tests and CI.

Writes ``<outbox>/<idempotency_key>.eml`` plus each attachment under
``<outbox>/<idempotency_key>/`` instead of sending anything.
"""

from __future__ import annotations

from pathlib import Path

from uncorrelated_trading.clock import Clock
from uncorrelated_trading.errors import EmailSendError
from uncorrelated_trading.models import EmailMessage, SendReceipt
from uncorrelated_trading.report.email.base import build_mime


def _safe_name(name: str) -> str:
    """Return ``name`` if it is a plain file name, else raise (no path traversal)."""
    if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise EmailSendError("file", f"unsafe file name: {name!r}", retryable=False)
    return name


class FileSender:
    """Write messages to an outbox directory.

    Parameters
    ----------
    outbox : Path
        Directory to write into (created if missing).
    clock : Clock
        Source of the receipt timestamp.
    """

    name = "file"

    def __init__(self, outbox: Path, clock: Clock) -> None:
        self._outbox = outbox
        self._clock = clock

    def send(self, message: EmailMessage) -> SendReceipt:
        """Write ``message`` as an ``.eml`` file plus its attachments.

        Parameters
        ----------
        message : EmailMessage
            The message to write.

        Returns
        -------
        SendReceipt
            Receipt whose ``message_id`` is the ``.eml`` path as a string.

        Raises
        ------
        EmailSendError
            If the key or an attachment name is not a plain file name, or writing fails.
        """
        key = _safe_name(message.idempotency_key)
        eml_path = self._outbox / f"{key}.eml"
        attach_dir = self._outbox / key
        try:
            self._outbox.mkdir(parents=True, exist_ok=True)
            eml_path.write_bytes(bytes(build_mime(message)))
            if message.attachments:
                attach_dir.mkdir(parents=True, exist_ok=True)
            for attachment in message.attachments:
                (attach_dir / _safe_name(attachment.filename)).write_bytes(attachment.content)
        except OSError as exc:
            raise EmailSendError(self.name, f"cannot write outbox: {exc}", retryable=False) from exc
        return SendReceipt(backend=self.name, message_id=str(eml_path), sent_at=self._clock.now())
