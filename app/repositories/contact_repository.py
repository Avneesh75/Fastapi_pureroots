"""Contact message and newsletter queries."""
import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.contact import ContactMessage, NewsletterSubscriber


def get_message(db: Session, message_id: uuid.UUID) -> ContactMessage | None:
    return db.get(ContactMessage, message_id)


def list_unsynced(
    db: Session, *, include_pending_before: datetime | None = None, limit: int = 200
) -> list[ContactMessage]:
    """Messages whose Sheets sync failed (and, optionally, stale pending ones)."""
    condition = ContactMessage.sheets_sync_status == "failed"
    if include_pending_before is not None:
        condition = or_(
            condition,
            (ContactMessage.sheets_sync_status == "pending") & (ContactMessage.created_at < include_pending_before),
        )
    stmt = select(ContactMessage).where(condition).order_by(ContactMessage.created_at).limit(limit)
    return list(db.scalars(stmt))


def get_subscriber(db: Session, email: str) -> NewsletterSubscriber | None:
    return db.scalar(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email.strip().lower()))
