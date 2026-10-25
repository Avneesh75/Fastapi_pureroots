"""Newsletter subscriptions (idempotent)."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.contact import NewsletterSubscriber
from app.repositories import contact_repository


def subscribe(db: Session, email: str, source: str | None) -> NewsletterSubscriber:
    """Create or re-activate a subscriber. Subscribing twice is not an error."""
    normalized = email.strip().lower()
    subscriber = contact_repository.get_subscriber(db, normalized)
    if subscriber is not None:
        if not subscriber.is_active:
            subscriber.is_active = True
            db.commit()
        return subscriber
    subscriber = NewsletterSubscriber(email=normalized, is_active=True, source=(source or "")[:50])
    db.add(subscriber)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = contact_repository.get_subscriber(db, normalized)
        assert existing is not None
        return existing
    return subscriber
