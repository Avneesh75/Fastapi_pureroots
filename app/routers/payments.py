"""/api/v1/payments/razorpay — Checkout verification and the Razorpay webhook."""
from typing import Any

from fastapi import APIRouter, Request

from app.core.rate_limit import limiter
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession, OptionalUser, SettingsDep
from app.schemas.order import OrderDetail, RazorpayVerifyRequest
from app.services import order_service, payment_service

router = APIRouter(prefix="/payments/razorpay", tags=["Payments"])

SIGNATURE_HEADER = "X-Razorpay-Signature"


@router.post(
    "/verify",
    response_model=ApiResponse[OrderDetail],
    summary="Verify a Razorpay Checkout payment",
    description=(
        "Called by the storefront with the Razorpay Checkout handler response. Checks the signature "
        "(HMAC-SHA256 of `order_id|payment_id` with the key secret), that the Razorpay order belongs to "
        "this order, and that the payment's amount, currency and status (captured/authorized) match. "
        "Then marks the order paid and confirmed. Idempotent. Owner, or guest with the order's `email`. "
        "Mismatches return `400 PAYMENT_VERIFICATION_FAILED` and leave the order untouched."
    ),
    responses=error_responses(400, 401, 403, 404, 422, 503),
)
def verify_payment(
    payload: RazorpayVerifyRequest, user: OptionalUser, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    order = payment_service.verify_checkout_payment(
        db,
        order_number=payload.order_number,
        razorpay_order_id=payload.razorpay_order_id,
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_signature=payload.razorpay_signature,
        user=user,
        email=payload.email,
        settings=settings,
    )
    fresh = order_service.get_order_by_id(db, order.id)
    return envelope(order_service.build_order_detail(db, fresh, settings), message="Payment received. Your order is confirmed.")


@router.post(
    "/webhook",
    summary="Razorpay webhook",
    description=(
        "Configure in the Razorpay dashboard with events `payment.captured`, `payment.failed` and `order.paid`. "
        "Verified with `X-Razorpay-Signature` (HMAC-SHA256 of the raw body with `RAZORPAY_WEBHOOK_SECRET`). "
        "Returns 200 for every valid signature and 400 for an invalid one. Exempt from the CSRF header rule "
        "and from rate limits."
    ),
    responses=error_responses(400),
)
@limiter.exempt
async def razorpay_webhook(request: Request, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    raw_body = await request.body()
    result = payment_service.handle_webhook(db, raw_body, request.headers.get(SIGNATURE_HEADER), settings)
    return envelope(result)
