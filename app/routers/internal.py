"""/api/v1/internal — service-to-service endpoints (blocked by nginx from the public internet)."""
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession, Inventory, SettingsDep, require_internal_token
from app.schemas.loyalty import CoinsAdjustmentOut, InternalCoinsAdjust
from app.schemas.order import InternalStatusChange, InternalUpiVerify, OrderDetail
from app.services import loyalty_service, order_service, upi_service

router = APIRouter(prefix="/internal", tags=["Internal"], dependencies=[Depends(require_internal_token)])


@router.post(
    "/orders/{order_id}/status",
    response_model=ApiResponse[OrderDetail],
    summary="Change an order's status (Django admin)",
    description=(
        "Requires `X-Internal-Token`. Validates the transition against the state machine, calls the "
        "matching inventory action (cancel → release, shipped → commit, delivered→refunded → return), "
        "writes `order_status_history` and returns the updated order. Invalid transitions return 409. "
        "An unpaid Razorpay or direct-UPI order can't be moved to `confirmed` (409): UPI orders are confirmed "
        "through `upi-verify`."
    ),
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
def change_order_status(
    order_id: uuid.UUID, payload: InternalStatusChange, db: DbSession, inventory: Inventory, settings: SettingsDep
) -> dict[str, Any]:
    order = order_service.get_order_by_id(db, order_id)
    order = order_service.change_status(
        db, order, payload.status, note=payload.note, changed_by=payload.changed_by, inventory=inventory
    )
    return envelope(order_service.build_order_detail(db, order, settings), message="Order status updated.")


@router.post(
    "/orders/{order_id}/upi-verify",
    response_model=ApiResponse[OrderDetail],
    summary="Approve or reject a submitted direct-UPI payment (Django admin)",
    description=(
        "Requires `X-Internal-Token`. `{decision: approve|reject, note?, changed_by}`. Staff check the UTR in "
        "the store's bank/UPI app first. **approve**: the payment is marked paid, `payment_status` `paid` and "
        "the order moves `pending → confirmed` (history \"UPI payment confirmed by admin:…\"). **reject**: the "
        "payment is marked rejected, `payment_status` `failed` and the order stays `pending` so the customer can "
        "submit another UTR (or it expires); the note is the reason shown to the customer. Both are "
        "idempotent. 409 when the order is not a UPI order awaiting verification."
    ),
    responses=error_responses(401, 403, 404, 409, 422),
)
def verify_upi_payment(
    order_id: uuid.UUID, payload: InternalUpiVerify, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    order = upi_service.verify_by_staff(
        db, order_id, decision=payload.decision, note=payload.note, changed_by=payload.changed_by
    )
    fresh = order_service.get_order_by_id(db, order.id)
    message = "UPI payment confirmed." if payload.decision == "approve" else "UPI payment rejected."
    return envelope(order_service.build_order_detail(db, fresh, settings), message=message)


@router.post(
    "/customers/{user_id}/coins",
    response_model=ApiResponse[CoinsAdjustmentOut],
    summary="Adjust a customer's PureRoots Coins (Django admin)",
    description=(
        "Requires `X-Internal-Token`. `{coins (signed, non-zero, |coins| ≤ 100000), note (3-300 chars), "
        "changed_by}` writes an `adjustment` ledger row and changes the available balance. 404 for an unknown "
        "customer; 409 when a deduction would take the available balance below zero."
    ),
    responses=error_responses(401, 403, 404, 409, 422),
)
def adjust_coins(user_id: uuid.UUID, payload: InternalCoinsAdjust, db: DbSession) -> dict[str, Any]:
    result = loyalty_service.adjust(db, user_id, payload.coins, payload.note, payload.changed_by)
    return envelope(loyalty_service.adjustment_payload(result), message="Coins adjusted.")
