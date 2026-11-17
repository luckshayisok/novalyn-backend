"""Email senders.

`console`, `file` and `memory` are development adapters: they never deliver
mail. `smtp` is the production adapter.
"""

import json
import logging
import smtplib
import uuid
from email.message import EmailMessage as MimeMessage
from functools import lru_cache
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.time import utcnow
from app.integrations.email.base import EmailMessage, EmailSender

logger = logging.getLogger(__name__)


class ConsoleEmailSender:
    """Development adapter: writes the message to the log."""

    def send(self, message: EmailMessage) -> None:
        logger.info("[dev email] to=%s subject=%s\n%s", message.to, message.subject, message.text)


class FileEmailSender:
    """Development adapter: writes each message as a JSON file."""

    def __init__(self, directory: str) -> None:
        self._directory = Path(directory)

    def send(self, message: EmailMessage) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        path = self._directory / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps(message.to_dict(), indent=2), encoding="utf-8")
        logger.info("[dev email] written to %s", path)


class MemoryEmailSender:
    """Test adapter: keeps messages in a list."""

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise RuntimeError("SMTP_HOST is required when EMAIL_BACKEND=smtp")
        self._settings = settings

    def send(self, message: EmailMessage) -> None:
        settings = self._settings
        mime = MimeMessage()
        mime["From"] = settings.email_from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
            if settings.smtp_use_tls:
                client.starttls()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password or "")
            client.send_message(mime)


@lru_cache
def get_email_sender() -> EmailSender:
    settings = get_settings()
    match settings.email_backend:
        case "smtp":
            return SmtpEmailSender(settings)
        case "file":
            return FileEmailSender(settings.email_file_dir)
        case "memory":
            return MemoryEmailSender()
        case _:
            return ConsoleEmailSender()
