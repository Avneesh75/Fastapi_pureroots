"""/api/v1/contact — "Contact us" enquiries (saved, then synced to Google Sheets in the background)."""
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, status

from app.core.rate_limit import CONTACT_LIMIT, client_ip, limiter
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession, LeadSinkDep
from app.schemas.contact import ContactCreate, ContactOut
from app.services import contact_service

router = APIRouter(prefix="/contact", tags=["Contact"])


@router.post(
    "",
    response_model=ApiResponse[ContactOut],
    status_code=status.HTTP_201_CREATED,
    summary="Send an enquiry",
    description=(
        "Name 2–120 chars, message 10–3000 chars, mobile `^[6-9]\\d{9}$`. The enquiry is stored first and "
        "pushed to Google Sheets in a background task; a Sheets failure never fails this request. "
        "Rate-limited to 5/minute per IP."
    ),
    responses=error_responses(403, 422, 429),
)
@limiter.limit(CONTACT_LIMIT)
def create_contact(
    request: Request,
    payload: ContactCreate,
    background_tasks: BackgroundTasks,
    db: DbSession,
    sink: LeadSinkDep,
) -> dict[str, Any]:
    message = contact_service.create_message(
        db,
        payload,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        sink_enabled=sink.enabled,
    )
    if sink.enabled:
        background_tasks.add_task(contact_service.sync_message, message.id, sink)
    return envelope(
        ContactOut(id=message.id, created_at=message.created_at),
        message="Thank you for reaching out! Our team will get back to you within one business day.",
    )
