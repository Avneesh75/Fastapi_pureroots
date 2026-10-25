"""Contact enquiries: save first, then sync to the configured ``LeadSink`` in the background."""
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy.orm import Session

from app import database
from app.core.clock import utcnow
from app.models.contact import ContactMessage
from app.repositories import contact_repository
from app.schemas.contact import ContactCreate
from app.services.lead_sink import LeadRecord, LeadSink

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
MAX_ERROR_LENGTH = 1000


def create_message(
    db: Session,
    payload: ContactCreate,
    *,
    ip_address: str | None,
    user_agent: str | None,
    sink_enabled: bool,
) -> ContactMessage:
    message = ContactMessage(
        full_name=payload.full_name,
        email=payload.email,
        mobile=payload.mobile,
        subject=payload.subject,
        message=payload.message,
        product_interest=payload.product_interest or "",
        city=payload.city or "",
        status="new",
        sheets_sync_status="pending" if sink_enabled else "disabled",
        ip_address=(ip_address or None) and ip_address[:45],
        user_agent=(user_agent or "")[:255],
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    logger.info("Contact message %s saved (sheets: %s)", message.id, message.sheets_sync_status)
    return message


def push_message(db: Session, message: ContactMessage, sink: LeadSink) -> str:
    """Push one message and record the outcome. Never raises."""
    try:
        sink.push(LeadRecord.from_message(message))
    except Exception as exc:  # noqa: BLE001 - failures are recorded, never surfaced to the customer
        message.sheets_sync_status = "failed"
        message.sheets_error = str(exc)[:MAX_ERROR_LENGTH] or type(exc).__name__
        logger.warning("Sheets sync failed for contact message %s: %s", message.id, type(exc).__name__)
    else:
        message.sheets_sync_status = "synced"
        message.sheets_error = None
        message.sheets_synced_at = utcnow()
    db.commit()
    return message.sheets_sync_status


def sync_message(message_id: uuid.UUID, sink: LeadSink, session_factory: SessionFactory | None = None) -> str | None:
    """Background task entry point: opens its own session (the request session is closed)."""
    factory = session_factory or database.SessionLocal
    db = factory()
    try:
        message = contact_repository.get_message(db, message_id)
        if message is None or message.sheets_sync_status not in ("pending", "failed"):
            return message.sheets_sync_status if message else None
        return push_message(db, message, sink)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while syncing contact message %s", message_id)
        db.rollback()
        return None
    finally:
        db.close()


def retry_unsynced(
    sink: LeadSink,
    *,
    include_stale_pending: bool = True,
    limit: int = 200,
    session_factory: SessionFactory | None = None,
) -> dict[str, int]:
    """Retry failed (and pending > 10 minutes old) messages. Returns counts per outcome."""
    counts = {"synced": 0, "failed": 0}
    if not sink.enabled:
        logger.warning("Lead sink is disabled; nothing to retry")
        return counts
    factory = session_factory or database.SessionLocal
    db = factory()
    try:
        stale_before = utcnow() - timedelta(minutes=10) if include_stale_pending else None
        for message in contact_repository.list_unsynced(db, include_pending_before=stale_before, limit=limit):
            outcome = push_message(db, message, sink)
            counts[outcome] = counts.get(outcome, 0) + 1
    finally:
        db.close()
    return counts
