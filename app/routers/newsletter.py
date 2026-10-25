"""/api/v1/newsletter — idempotent subscription."""
from typing import Any

from fastapi import APIRouter

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession
from app.schemas.contact import NewsletterOut, NewsletterSubscribe
from app.services import newsletter_service

router = APIRouter(prefix="/newsletter", tags=["Newsletter"])


@router.post(
    "/subscribe",
    response_model=ApiResponse[NewsletterOut],
    summary="Subscribe to the newsletter",
    description="Idempotent: subscribing an existing email returns 200 again.",
    responses=error_responses(403, 422),
)
def subscribe(payload: NewsletterSubscribe, db: DbSession) -> dict[str, Any]:
    subscriber = newsletter_service.subscribe(db, payload.email, payload.source)
    return envelope(
        NewsletterOut(email=subscriber.email, subscribed=subscriber.is_active),
        message="You're on the list! Look out for seasonal harvests and recipes.",
    )
