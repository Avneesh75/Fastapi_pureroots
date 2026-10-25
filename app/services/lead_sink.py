"""Where contact enquiries are pushed after being saved (Google Sheets or nowhere)."""
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from app.config import Settings, get_settings
from app.core.clock import IST, ensure_aware

if TYPE_CHECKING:
    from app.models.contact import ContactMessage

logger = logging.getLogger(__name__)

LEAD_STATUS_NEW = "New"
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class LeadSinkError(Exception):
    """Raised by a sink when a lead could not be delivered."""


def sheet_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection (values are sent with USER_ENTERED)."""
    text = value or ""
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


@dataclass(frozen=True)
class LeadRecord:
    created_at: datetime
    full_name: str
    email: str
    mobile: str
    subject: str
    product_interest: str
    city: str
    message: str

    @classmethod
    def from_message(cls, message: "ContactMessage") -> "LeadRecord":
        return cls(
            created_at=message.created_at,
            full_name=message.full_name,
            email=message.email,
            mobile=message.mobile,
            subject=message.subject,
            product_interest=message.product_interest or "",
            city=message.city or "",
            message=message.message,
        )

    @property
    def timestamp(self) -> str:
        created = ensure_aware(self.created_at)
        assert created is not None
        return created.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")

    def to_row(self) -> list[str]:
        """Row order: Timestamp, Name, Email, Mobile, Subject, Product, City, Message, Status."""
        return [
            self.timestamp,
            sheet_safe(self.full_name),
            sheet_safe(self.email),
            sheet_safe(self.mobile),
            sheet_safe(self.subject),
            sheet_safe(self.product_interest),
            sheet_safe(self.city),
            sheet_safe(self.message),
            LEAD_STATUS_NEW,
        ]


class LeadSink(Protocol):
    enabled: bool

    def push(self, lead: LeadRecord) -> None: ...


class NullLeadSink:
    """Used when ``GOOGLE_SHEETS_ENABLED=false``; messages are marked ``disabled``."""

    enabled = False

    def push(self, lead: LeadRecord) -> None:
        return None


def build_lead_sink(settings: Settings) -> LeadSink:
    if not settings.google_sheets_enabled:
        return NullLeadSink()
    if not settings.google_sheets_configured:
        logger.warning(
            "GOOGLE_SHEETS_ENABLED=true but spreadsheet id / service account / private key are missing; "
            "contact enquiries will not be synced"
        )
        return NullLeadSink()
    from app.services.google_sheets_service import GoogleSheetsLeadSink  # local import avoids a cycle

    return GoogleSheetsLeadSink.from_settings(settings)


@lru_cache
def get_default_lead_sink() -> LeadSink:
    return build_lead_sink(get_settings())
