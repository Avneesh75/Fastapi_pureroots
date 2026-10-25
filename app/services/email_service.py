"""Outgoing email. ``ConsoleEmailSender`` (dev) logs messages; ``SmtpEmailSender`` when ``SMTP_HOST`` is set."""
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import parseaddr
from functools import lru_cache
from typing import Protocol
from urllib.parse import quote

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    def send(self, message: OutgoingEmail) -> None: ...


class ConsoleEmailSender:
    """Development sender: writes the email (including reset links) to the log."""

    def send(self, message: OutgoingEmail) -> None:
        logger.info("[console-email] To: %s | Subject: %s\n%s", message.to, message.subject, message.body)


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        from_address: str,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._from = from_address
        self._timeout = timeout

    def send(self, message: OutgoingEmail) -> None:
        mime = MimeMessage()
        mime["From"] = self._from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(mime)
        logger.info("Email '%s' sent to %s", message.subject, message.to)


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.smtp_host:
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            from_address=settings.email_from,
        )
    return ConsoleEmailSender()


@lru_cache
def get_default_email_sender() -> EmailSender:
    return build_email_sender(get_settings())


def send_safely(sender: EmailSender, message: OutgoingEmail) -> None:
    """Send and swallow errors (used from background tasks)."""
    try:
        sender.send(message)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send email '%s' to %s", message.subject, message.to)


def reset_link(settings: Settings, token: str) -> str:
    return f"{settings.public_site_url.rstrip('/')}/account/reset-password/?token={quote(token, safe='')}"


def password_reset_email(to: str, full_name: str, link: str, expire_minutes: int) -> OutgoingEmail:
    first_name = (full_name or "").split(" ")[0] or "there"
    brand = parseaddr(get_settings().email_from)[0] or "PureRoots"
    body = (
        f"Hi {first_name},\n\n"
        f"We received a request to reset your {brand} password. Use the link below to choose a new one.\n\n"
        f"{link}\n\n"
        f"This link expires in {expire_minutes} minutes and can be used once. "
        "If you didn't ask for this, you can safely ignore this email.\n\n"
        f"— Team {brand}\n"
    )
    return OutgoingEmail(to=to, subject=f"Reset your {brand} password", body=body)
