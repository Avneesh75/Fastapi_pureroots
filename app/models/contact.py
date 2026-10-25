"""Contact enquiries and newsletter subscribers."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ContactMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A "Contact us" enquiry. Django may only write ``status``."""

    __tablename__ = "contact_messages"

    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254))
    mobile: Mapped[str] = mapped_column(String(15))
    subject: Mapped[str] = mapped_column(String(150))
    message: Mapped[str] = mapped_column(Text)
    product_interest: Mapped[str] = mapped_column(String(200), default="", server_default="")
    city: Mapped[str] = mapped_column(String(100), default="", server_default="")
    status: Mapped[str] = mapped_column(String(20), default="new", server_default="new", index=True)
    sheets_sync_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    sheets_error: Mapped[str | None] = mapped_column(Text)
    sheets_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str] = mapped_column(String(255), default="", server_default="")


class NewsletterSubscriber(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "newsletter_subscribers"

    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    source: Mapped[str] = mapped_column(String(50), default="", server_default="")
