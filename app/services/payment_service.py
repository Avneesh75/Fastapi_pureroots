"""Online payments (Razorpay): checkout payload, verification, webhooks, retries, refunds and expiry.

Order/payment states for an online order (ARCHITECTURE §4.3, §5)::

    placed            → orders.status=pending,   payment_status=pending,  payments.status=created
    verified/captured → orders.status=confirmed, payment_status=paid,     payments.status=paid
    payment.failed    → orders.status=pending,   payment_status=failed,   payments.status=failed (retry allowed)
    not paid in time  → orders.status=cancelled  (stock released)
    cancel / refund   → Razorpay refund first, then payment_status=refunded, payments.status=refunded

``mark_paid`` is the single, idempotent way an order becomes paid; the checkout handler
(``/payments/razorpay/verify``), the webhook and the expiry reconciliation all go through it.
It locks the order row (``SELECT … FOR UPDATE``) so concurrent verify + webhook calls are safe.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.core.clock import ensure_aware, utcnow
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PaymentVerificationError,
    RefundFailedError,
    ServiceUnavailableError,
    WebhookSignatureError,
)
from app.models.order import Order, OrderStatusHistory, Payment
from app.models.user import User
from app.repositories import order_repository
from app.services import loyalty_service
from app.services.inventory_client import InventoryGateway
from app.services.payments.razorpay import (
    CHECKOUT_NAME,
    CURRENCY,
    RazorpayAPIError,
    RazorpayProvider,
    sanitize_entity,
    to_paise,
)

logger = logging.getLogger(__name__)

PROVIDER_CODE = "razorpay"
PAYABLE_PAYMENT_STATUSES = frozenset({"pending", "failed"})
ACCEPTED_PAYMENT_STATES = frozenset({"captured", "authorized"})
REUSABLE_RZP_ORDER_STATES = frozenset({"created", "attempted"})
OPEN_ROW_STATUSES = frozenset({"created", "failed"})

PAID_NOTE = "Payment received via Razorpay"
EXPIRED_NOTE = "Payment not completed in time"
ORDER_NOT_FOUND = "We couldn't find an order with those details."
NOT_CONFIGURED = "Online payments are not available right now."
NOT_PAYABLE = "This order can't be paid online."


# --------------------------------------------------------------------------- helpers


def get_provider(settings: Settings | None = None) -> RazorpayProvider:
    """The configured Razorpay provider, or 503 when the keys are missing."""
    settings = settings or get_settings()
    if not settings.razorpay_configured:
        raise ServiceUnavailableError(NOT_CONFIGURED)
    return RazorpayProvider.from_settings(settings)


def is_online(order: Order) -> bool:
    return order.payment_method == PROVIDER_CODE


def can_pay(order: Order) -> bool:
    """A pending Razorpay order that has not been paid yet (the customer may (re)try)."""
    return is_online(order) and order.status == "pending" and order.payment_status in PAYABLE_PAYMENT_STATUSES


def _raw(row: Payment) -> dict[str, Any]:
    return dict(row.raw_response or {})


def _set_raw(row: Payment, **changes: Any) -> None:
    """Assign a NEW dict so SQLAlchemy notices the JSON change."""
    row.raw_response = {**_raw(row), **changes}


def _rows(order: Order) -> list[Payment]:
    return [row for row in order.payments if row.provider == PROVIDER_CODE]


def _row_for_rzp_order(order: Order, rzp_order_id: str) -> Payment | None:
    for row in _rows(order):
        if rzp_order_id and row.provider_reference == rzp_order_id:
            return row
    return None


def _latest_open_row(order: Order) -> Payment | None:
    open_rows = [row for row in _rows(order) if row.status in OPEN_ROW_STATUSES]
    return open_rows[-1] if open_rows else None


def _paid_row(order: Order) -> Payment | None:
    for row in reversed(_rows(order)):
        if row.status == "paid":
            return row
    return None


def _history(order: Order, to_status: str, note: str, changed_by: str = "system") -> None:
    order.status_history.append(
        OrderStatusHistory(from_status=order.status, to_status=to_status, note=note, changed_by=changed_by)
    )
    order.status = to_status


def _lock_order(db: Session, order_id: uuid.UUID) -> Order:
    """Re-read the order with a row lock (a no-op on SQLite)."""
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items), selectinload(Order.status_history), selectinload(Order.payments))
        .with_for_update(of=Order)
        .execution_options(populate_existing=True)
    )
    order = db.scalars(stmt).one_or_none()
    if order is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    return order


def _inr(paise: int) -> str:
    return f"₹{Decimal(paise) / 100:,.2f}"


def get_accessible_order(db: Session, order_number: str, user: User | None, email: str | None) -> Order:
    """The order, for its signed-in owner or a guest who knows the order's email (404 otherwise)."""
    order = order_repository.get_by_number(db, order_number or "")
    if order is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    if user is not None and order.user_id == user.id:
        return order
    if email and order.customer_email.lower() == email.strip().lower():
        return order
    raise NotFoundError(ORDER_NOT_FOUND)


def checkout_payload(order: Order, row: Payment, settings: Settings) -> dict[str, Any]:
    """What the browser needs to open Razorpay Checkout (public key id only, never the secret)."""
    return {
        "provider": PROVIDER_CODE,
        "key_id": settings.razorpay_key_id,
        "razorpay_order_id": row.provider_reference,
        "amount": to_paise(order.grand_total),
        "currency": row.currency or CURRENCY,
        "name": CHECKOUT_NAME,
        "description": f"Order {order.order_number}",
        "prefill": {
            "name": order.customer_name,
            "email": order.customer_email,
            "contact": f"+91{order.customer_phone}" if order.customer_phone else "",
        },
        "order_number": order.order_number,
    }


def payload_for_new_order(order: Order, settings: Settings) -> dict[str, Any] | None:
    """Checkout response ``payment`` block for an order that was just placed with Razorpay."""
    if not can_pay(order):
        return None
    row = _latest_open_row(order)
    return checkout_payload(order, row, settings) if row is not None else None


def _mismatches(entity: dict[str, Any], row: Payment) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    if entity.get("order_id") != row.provider_reference:
        problems.append({"field": "razorpay_order_id", "message": "The payment belongs to a different order."})
    if entity.get("amount") != to_paise(row.amount):
        problems.append({"field": "amount", "message": "The paid amount does not match the order total."})
    if str(entity.get("currency", "")).upper() != (row.currency or CURRENCY).upper():
        problems.append({"field": "currency", "message": "The payment currency does not match."})
    if entity.get("status") not in ACCEPTED_PAYMENT_STATES:
        problems.append({"field": "status", "message": "The payment has not been completed."})
    return problems


# --------------------------------------------------------------------------- mark paid (idempotent)


def mark_paid(
    db: Session,
    order_id: uuid.UUID,
    payment: dict[str, Any],
    provider: RazorpayProvider,
    *,
    source: str,
) -> Order:
    """Record a verified, captured (or authorized) Razorpay payment. Safe to call repeatedly.

    ``payment`` is a Razorpay payment entity whose order id, amount and currency were already
    checked against the ``payments`` row by the caller.
    """
    order = _lock_order(db, order_id)
    payment_id = str(payment.get("id", ""))
    row = _row_for_rzp_order(order, str(payment.get("order_id", "")))
    if row is None or not payment_id:
        db.rollback()
        raise PaymentVerificationError("This payment does not belong to this order.")

    # Already recorded (e.g. verify after webhook, or a webhook retry): nothing to do.
    if _raw(row).get("razorpay_payment_id") == payment_id and row.status in {"paid", "refunded"}:
        db.rollback()
        return order

    amount_paise = to_paise(row.amount)

    # A second, different successful payment for an order that is already paid: refund it.
    if order.payment_status in {"paid", "refunded"}:
        _refund_duplicate(db, order, row, payment_id, amount_paise, provider)
        return order

    if payment.get("status") == "authorized":
        payment = provider.capture_payment(payment_id, amount_paise, row.currency or CURRENCY)

    row.status = "paid"
    _set_raw(
        row,
        razorpay_order_id=row.provider_reference,
        razorpay_payment_id=payment_id,
        payment=sanitize_entity(payment),
        paid_via=source,
        paid_at=utcnow().isoformat(),
    )
    order.payment_status = "paid"

    if order.status == "pending":
        _history(order, "confirmed", PAID_NOTE)
    elif order.status == "cancelled":
        # Paid after the order was cancelled (e.g. it expired): give the money back.
        _refund_late_payment(order, row, payment_id, amount_paise, provider)
    db.commit()
    logger.info("Order %s paid via Razorpay (%s, %s)", order.order_number, payment_id, source)
    return order


def _refund_duplicate(
    db: Session, order: Order, row: Payment, payment_id: str, amount_paise: int, provider: RazorpayProvider
) -> None:
    duplicates = list(_raw(row).get("duplicate_payments") or [])
    if any(entry.get("payment_id") == payment_id for entry in duplicates):
        db.rollback()
        return
    entry: dict[str, Any] = {"payment_id": payment_id, "amount": amount_paise}
    try:
        entry["refund"] = sanitize_entity(provider.refund(payment_id, amount_paise))
        note = f"Duplicate payment {payment_id} refunded automatically"
    except RazorpayAPIError:
        entry["refund_error"] = "refund failed; refund manually from the Razorpay dashboard"
        note = f"Duplicate payment {payment_id} received; automatic refund failed, refund it manually"
    _set_raw(row, duplicate_payments=[*duplicates, entry])
    order.status_history.append(
        OrderStatusHistory(from_status=order.status, to_status=order.status, note=note, changed_by="system")
    )
    db.commit()
    logger.warning("Order %s: %s", order.order_number, note)


def _refund_late_payment(
    order: Order, row: Payment, payment_id: str, amount_paise: int, provider: RazorpayProvider
) -> None:
    try:
        refund = provider.refund(payment_id, amount_paise)
    except RazorpayAPIError:
        # payment_status stays "paid" so an admin can move cancelled → refunded to retry.
        note = f"{PAID_NOTE} after cancellation; automatic refund failed, please refund"
        logger.error("Order %s: late payment %s could not be refunded", order.order_number, payment_id)
    else:
        _record_refund(order, row, refund)
        note = f"{PAID_NOTE} after cancellation; refunded {_inr(amount_paise)} ({refund.get('id', '')})"
    order.status_history.append(
        OrderStatusHistory(from_status=order.status, to_status=order.status, note=note, changed_by="system")
    )


def _record_refund(order: Order, row: Payment, refund: dict[str, Any]) -> None:
    refunds = list(_raw(row).get("refunds") or [])
    _set_raw(row, refunds=[*refunds, sanitize_entity(refund)])
    row.status = "refunded"
    order.payment_status = "refunded"


# --------------------------------------------------------------------------- checkout verify


def verify_checkout_payment(
    db: Session,
    *,
    order_number: str,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    user: User | None,
    email: str | None,
    settings: Settings,
) -> Order:
    """Verify the Razorpay Checkout handler response and mark the order paid."""
    order = get_accessible_order(db, order_number, user, email)
    provider = get_provider(settings)

    row = _row_for_rzp_order(order, razorpay_order_id)
    if row is None:
        raise PaymentVerificationError(
            "This payment does not belong to this order.",
            details=[{"field": "razorpay_order_id", "message": "Unknown Razorpay order for this order."}],
        )
    if not provider.verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
        raise PaymentVerificationError(details=[{"field": "razorpay_signature", "message": "Invalid signature."}])

    if _raw(row).get("razorpay_payment_id") == razorpay_payment_id and row.status in {"paid", "refunded"}:
        return order  # already verified (idempotent)

    payment = provider.fetch_payment(razorpay_payment_id)
    problems = _mismatches(payment, row)
    if problems:
        logger.warning("Order %s: Razorpay payment %s failed verification: %s", order.order_number, razorpay_payment_id, problems)
        raise PaymentVerificationError(details=problems)
    return mark_paid(db, order.id, payment, provider, source="checkout")


# --------------------------------------------------------------------------- webhook


def handle_webhook(db: Session, raw_body: bytes, signature: str | None, settings: Settings) -> dict[str, Any]:
    """Verify ``X-Razorpay-Signature`` and apply ``payment.captured`` / ``order.paid`` / ``payment.failed``.

    Invalid signature → ``WebhookSignatureError`` (400). Anything else returns normally (200),
    even when processing fails, so Razorpay does not hammer us; failures are logged and the
    expiry sweep reconciles with Razorpay before cancelling anything.
    """
    provider = RazorpayProvider.from_settings(settings)
    if not provider.verify_webhook_signature(raw_body, signature):
        raise WebhookSignatureError()
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Razorpay webhook with a valid signature but an unreadable body")
        return {"event": None, "handled": False}
    name = str(event.get("event", "")) if isinstance(event, dict) else ""
    payload = event.get("payload") if isinstance(event, dict) else None
    entity = ((payload or {}).get("payment") or {}).get("entity") if isinstance(payload, dict) else None
    if not isinstance(entity, dict):
        return {"event": name, "handled": False}

    try:
        if name in {"payment.captured", "order.paid"}:
            handled = _apply_captured(db, entity, provider, source=f"webhook:{name}")
        elif name == "payment.failed":
            handled = _apply_failed(db, entity)
        else:
            handled = False
    except Exception:  # noqa: BLE001 - never fail the webhook response for a valid signature
        db.rollback()
        logger.exception("Razorpay webhook %s could not be processed", name)
        handled = False
    return {"event": name, "handled": handled}


def _row_by_reference(db: Session, rzp_order_id: str) -> Payment | None:
    if not rzp_order_id:
        return None
    return db.scalar(
        select(Payment).where(Payment.provider == PROVIDER_CODE, Payment.provider_reference == rzp_order_id)
    )


def _apply_captured(db: Session, entity: dict[str, Any], provider: RazorpayProvider, *, source: str) -> bool:
    row = _row_by_reference(db, str(entity.get("order_id", "")))
    if row is None:
        logger.info("Razorpay webhook for unknown order %s ignored", entity.get("order_id"))
        return False
    problems = _mismatches(entity, row)
    if problems:
        logger.error("Razorpay webhook payment %s does not match its order: %s", entity.get("id"), problems)
        return False
    mark_paid(db, row.order_id, entity, provider, source=source)
    return True


def _apply_failed(db: Session, entity: dict[str, Any]) -> bool:
    row = _row_by_reference(db, str(entity.get("order_id", "")))
    if row is None:
        return False
    order = _lock_order(db, row.order_id)
    row = _row_for_rzp_order(order, row.provider_reference)
    if row is None or order.payment_status in {"paid", "refunded"} or row.status in {"paid", "refunded"}:
        db.rollback()
        return False
    row.status = "failed"
    _set_raw(
        row,
        last_error={
            "payment_id": entity.get("id"),
            "error_code": entity.get("error_code"),
            "error_description": entity.get("error_description"),
            "error_reason": entity.get("error_reason"),
        },
    )
    if order.status == "pending":
        order.payment_status = "failed"
    db.commit()
    logger.info("Order %s: Razorpay payment %s failed", order.order_number, entity.get("id"))
    return True


# --------------------------------------------------------------------------- retry


def _deadline(order: Order, settings: Settings) -> datetime:
    placed = ensure_aware(order.placed_at) or utcnow()
    return placed + timedelta(minutes=settings.payment_pending_expiry_minutes)


def reconcile(db: Session, order: Order, provider: RazorpayProvider) -> bool:
    """Ask Razorpay whether any open Razorpay order of ``order`` was paid; mark it paid if so."""
    for row in [row for row in _rows(order) if row.status in OPEN_ROW_STATUSES and row.provider_reference]:
        for payment in provider.fetch_order_payments(row.provider_reference):
            if payment.get("status") in ACCEPTED_PAYMENT_STATES and not _mismatches(payment, row):
                mark_paid(db, order.id, payment, provider, source="reconcile")
                return True
    return False


def retry_payment(
    db: Session, order_number: str, user: User | None, email: str | None, settings: Settings
) -> tuple[Order, dict[str, Any]]:
    """Payment payload for a pending, unpaid Razorpay order (reusing its Razorpay order when possible)."""
    order = get_accessible_order(db, order_number, user, email)
    if not can_pay(order):
        if is_online(order) and order.payment_status == "paid":
            raise ConflictError("This order has already been paid.")
        raise ConflictError(NOT_PAYABLE if not is_online(order) else "This order is no longer awaiting payment.")
    if utcnow() >= _deadline(order, settings):
        raise ConflictError("The time to pay for this order has run out. Please place a new order.")
    provider = get_provider(settings)
    order = _lock_order(db, order.id)
    if not can_pay(order):
        db.rollback()
        raise ConflictError("This order is no longer awaiting payment.")

    amount_paise = to_paise(order.grand_total)
    row = _latest_open_row(order)
    reusable = False
    if row is not None and row.provider_reference:
        rzp_order = provider.fetch_order(row.provider_reference)
        if rzp_order.get("status") == "paid":
            db.rollback()
            reconcile(db, order_repository.get_by_id(db, order.id) or order, provider)
            raise ConflictError("This order has already been paid.")
        reusable = rzp_order.get("status") in REUSABLE_RZP_ORDER_STATES and rzp_order.get("amount") == amount_paise

    if reusable and row is not None:
        row.status = "created"
    else:
        created = provider.create_order(order)
        row = Payment(
            provider=PROVIDER_CODE,
            provider_reference=str(created["id"]),
            amount=order.grand_total,
            currency=CURRENCY,
            status="created",
            raw_response={"razorpay_order": sanitize_entity(created)},
        )
        order.payments.append(row)
    order.payment_status = "pending"
    db.commit()
    return order, checkout_payload(order, row, settings)


# --------------------------------------------------------------------------- refunds (status changes)


def refund_for_transition(order: Order, to_status: str, settings: Settings | None = None) -> str | None:
    """Refund a paid Razorpay order that is being cancelled or refunded. Returns a history note.

    Called BEFORE the status change is committed. On failure it raises and changes nothing:
    ``RefundFailedError`` (409) when Razorpay rejects the refund, ``ServiceUnavailableError``
    (503) when Razorpay can't be reached or isn't configured.
    """
    if not is_online(order) or order.payment_status != "paid" or to_status not in {"cancelled", "refunded"}:
        return None
    row = _paid_row(order)
    payment_id = str(_raw(row).get("razorpay_payment_id") or "") if row is not None else ""
    if row is None or not payment_id:
        raise RefundFailedError("No captured Razorpay payment was found for this order, so it can't be refunded.")
    settings = settings or get_settings()
    if not settings.razorpay_configured:
        raise ServiceUnavailableError(
            "Razorpay is not configured, so the refund can't be issued. The order was not changed."
        )
    provider = RazorpayProvider.from_settings(settings)
    amount_paise = to_paise(row.amount)
    try:
        refund = provider.refund(payment_id, amount_paise)
    except RazorpayAPIError as exc:
        if exc.is_client_error:
            reason = f" ({exc.provider_description})" if exc.provider_description else ""
            raise RefundFailedError(f"Razorpay rejected the refund{reason}. The order was not changed.") from exc
        raise ServiceUnavailableError(
            "We couldn't reach Razorpay to issue the refund. The order was not changed; please try again."
        ) from exc
    _record_refund(order, row, refund)
    logger.info("Order %s: refunded %s paise via Razorpay (%s)", order.order_number, amount_paise, refund.get("id"))
    return f"Refund of {_inr(amount_paise)} issued via Razorpay ({refund.get('id', '')})"


# --------------------------------------------------------------------------- expiry


def expire_unpaid_orders(
    db: Session,
    inventory: InventoryGateway,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Cancel Razorpay orders left unpaid for ``PAYMENT_PENDING_EXPIRY_MINUTES``; release their stock.

    Safe with several workers: each order is claimed by a conditional ``UPDATE … WHERE
    status='pending' AND payment_status IN (…)``; only the worker whose UPDATE hits the row goes on,
    and the row lock is held until its commit. Before cancelling, Razorpay is asked whether the
    order was paid after all (a missed webhook) and such orders are marked paid instead.
    """
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.payment_pending_expiry_minutes)
    candidates = list(
        db.scalars(
            select(Order.id)
            .where(
                Order.payment_method == PROVIDER_CODE,
                Order.status == "pending",
                Order.payment_status.in_(PAYABLE_PAYMENT_STATUSES),
                Order.placed_at < cutoff,
            )
            .order_by(Order.placed_at)
            .limit(limit)
        )
    )
    db.rollback()
    provider = RazorpayProvider.from_settings(settings) if settings.razorpay_configured else None
    expired = 0
    for order_id in candidates:
        try:
            if _expire_one(db, order_id, inventory, provider, now):
                expired += 1
        except Exception:  # noqa: BLE001 - one bad order must not stop the sweep
            db.rollback()
            logger.exception("Could not expire unpaid order %s", order_id)
    if expired:
        logger.info("Expired %s unpaid Razorpay order(s)", expired)
    return expired


def _expire_one(
    db: Session, order_id: uuid.UUID, inventory: InventoryGateway, provider: RazorpayProvider | None, now: datetime
) -> bool:
    if provider is not None:
        order = order_repository.get_by_id(db, order_id)
        if order is None or not can_pay(order):
            db.rollback()
            return False
        try:
            if reconcile(db, order, provider):
                return False
        except RazorpayAPIError:
            db.rollback()
            logger.warning("Razorpay unreachable; order %s will be checked again on the next sweep", order_id)
            return False
        db.rollback()

    claimed = db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "pending",
            Order.payment_status.in_(PAYABLE_PAYMENT_STATUSES),
        )
        .values(status="cancelled", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return False
    try:
        inventory.release(order_id)
    except Exception:
        db.rollback()
        raise
    db.add(
        OrderStatusHistory(
            order_id=order_id, from_status="pending", to_status="cancelled", note=EXPIRED_NOTE, changed_by="system"
        )
    )
    loyalty_service.reverse_by_order_id(db, order_id, reason=EXPIRED_NOTE)
    db.commit()
    return True
