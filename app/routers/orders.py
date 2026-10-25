"""/api/v1/orders — checkout, order history, guest lookup and cancellation."""
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.pagination import PageParams, page_meta, page_params
from app.core.rate_limit import UPI_SUBMIT_LIMIT, limiter
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CartCtx, CurrentUser, DbSession, Inventory, OptionalUser, SettingsDep
from app.schemas.order import (
    CheckoutRequest,
    OrderDetail,
    OrderLookupRequest,
    OrderPayRequest,
    OrderSummary,
    PaymentPayload,
    UpiPaymentPayload,
    UpiSubmitRequest,
)
from app.services import order_service, payment_service, upi_service
from app.services.pricing_service import format_inr

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.post(
    "",
    response_model=ApiResponse[OrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Place an order (checkout)",
    description=(
        "Re-prices the cart on the server, creates a `pending` order, reserves stock in Django, "
        "initiates payment (COD: nothing to do), confirms the order and clears the cart. "
        "With `payment_method=razorpay` the order stays `pending` (payment_status `pending`) and the "
        "response carries `payment{provider, key_id, razorpay_order_id, amount (paise), currency, name, "
        "description, prefill, order_number}` for Razorpay Checkout; if Razorpay can't be reached the "
        "stock is released, nothing is saved and 503 is returned. "
        "With `payment_method=upi` (direct UPI, offered when `UPI_VPA` is set) the order stays `pending` and the "
        "response carries `payment{provider:\"upi\", vpa, payee_name, amount (rupees), currency, upi_uri, note, "
        "order_number, expires_at}`; the customer pays that UPI ID and then submits the UTR. "
        "Guest checkout is allowed. Returns `409 INSUFFICIENT_STOCK` with "
        "`details[{variant_id, sku, requested, available}]` when stock is short."
    ),
    responses=error_responses(401, 403, 409, 422, 503),
)
def place_order(
    payload: CheckoutRequest, ctx: CartCtx, db: DbSession, inventory: Inventory, settings: SettingsDep
) -> dict[str, Any]:
    order = order_service.place_order(db, ctx, payload, inventory, settings)
    detail = order_service.build_order_detail(db, order, settings)
    upi_payment = upi_service.payload_for_new_order(order, settings)
    if upi_payment is not None:
        detail.payment = UpiPaymentPayload.model_validate(upi_payment)
        return envelope(
            detail, message=f"Order placed. Pay {format_inr(order.grand_total)} by UPI and share the UTR to confirm it."
        )
    payment = payment_service.payload_for_new_order(order, settings)
    if payment is not None:
        detail.payment = PaymentPayload.model_validate(payment)
        return envelope(detail, message="Order placed. Complete your payment to confirm it.")
    return envelope(detail, message="Thank you! Your order is confirmed.")


@router.get(
    "",
    response_model=ApiResponse[list[OrderSummary]],
    summary="My orders",
    description="Newest first, paginated.",
    responses=error_responses(401),
)
def list_orders(
    user: CurrentUser, db: DbSession, settings: SettingsDep, page: PageParams = Depends(page_params)
) -> dict[str, Any]:
    orders, total = order_service.list_user_orders(db, user, page.offset, page.page_size)
    summaries = order_service.build_order_summaries(db, orders, settings)
    return envelope(summaries, meta=page_meta(page.page, page.page_size, total))


@router.post(
    "/lookup",
    response_model=ApiResponse[OrderDetail],
    summary="Find an order by number and email",
    description="Used for guest tracking and the order success page.",
    responses=error_responses(404, 422),
)
def lookup_order(payload: OrderLookupRequest, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    order = order_service.lookup_order(db, payload.order_number, payload.email)
    return envelope(order_service.build_order_detail(db, order, settings))


@router.get(
    "/{order_number}",
    response_model=ApiResponse[OrderDetail],
    summary="Order detail (owner only)",
    description="Returns 404 for orders that do not belong to the signed-in customer.",
    responses=error_responses(401, 404),
)
def get_order(order_number: str, user: CurrentUser, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    order = order_service.get_user_order(db, user, order_number)
    return envelope(order_service.build_order_detail(db, order, settings))


@router.post(
    "/{order_number}/cancel",
    response_model=ApiResponse[OrderDetail],
    summary="Cancel my order",
    description="Allowed while the order is `pending`, `confirmed` or `processing`. Reserved stock is released.",
    responses=error_responses(401, 403, 404, 409, 503),
)
def cancel_order(
    order_number: str, user: CurrentUser, db: DbSession, inventory: Inventory, settings: SettingsDep
) -> dict[str, Any]:
    order = order_service.cancel_by_customer(db, user, order_number, inventory)
    return envelope(order_service.build_order_detail(db, order, settings), message="Your order has been cancelled.")


@router.post(
    "/{order_number}/pay",
    response_model=ApiResponse[OrderDetail],
    summary="Retry the online payment of a pending order",
    description=(
        "For a `pending`, unpaid Razorpay order: returns the order with a fresh `payment` payload, reusing "
        "the existing Razorpay order while it is still open or creating a new one. Owner, or guest with the "
        "order's `email`. `409` when the order is not payable (paid, cancelled, COD or past the payment window)."
    ),
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
def pay_order(
    order_number: str, payload: OrderPayRequest, user: OptionalUser, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    order, payment = payment_service.retry_payment(db, order_number, user, payload.email, settings)
    detail = order_service.build_order_detail(db, order_service.get_order_by_id(db, order.id), settings)
    detail.payment = PaymentPayload.model_validate(payment)
    return envelope(detail)


@router.post(
    "/{order_number}/upi-submit",
    response_model=ApiResponse[OrderDetail],
    summary="Submit the UPI transaction reference (UTR) of a direct-UPI payment",
    description=(
        "After paying the store's UPI ID, the customer sends the 12-digit UTR from their UPI app "
        "(`{utr, payer_vpa?, email?}`). The payment is recorded as submitted, `payment_status` becomes "
        "`awaiting_verification` and the order stays `pending` until staff confirm the money arrived. "
        "Owner, or guest with the order's `email` (404 otherwise). Allowed while the order is pending and "
        "unpaid (first submission, a correction while verification is pending, or after a rejection); "
        "re-sending the same UTR is a no-op. `422` for a malformed UTR, `409` when the UTR is already used "
        "by another payment or the order can't take a payment (paid, cancelled, not UPI)."
    ),
    responses=error_responses(401, 403, 404, 409, 422, 429),
)
@limiter.limit(UPI_SUBMIT_LIMIT)
def submit_upi_payment(
    request: Request,
    order_number: str,
    payload: UpiSubmitRequest,
    user: OptionalUser,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    order = upi_service.submit_utr(
        db,
        order_number=order_number,
        utr=payload.utr,
        payer_vpa=payload.payer_vpa,
        user=user,
        email=payload.email,
        settings=settings,
    )
    fresh = order_service.get_order_by_id(db, order.id)
    return envelope(
        order_service.build_order_detail(db, fresh, settings),
        message="Payment submitted. We'll confirm your order once we've verified it.",
    )
