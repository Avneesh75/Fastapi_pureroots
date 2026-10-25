"""Direct UPI payments: checkout payload, UTR submission, staff verification and expiry (ARCHITECTURE §4.3, §5).

Order/payment states of a UPI order::

    placed          → orders.status=pending,   payment_status=pending,               payments.status=awaiting_payment
    UTR submitted   → orders.status=pending,   payment_status=awaiting_verification, payments.status=submitted
    admin approves  → orders.status=confirmed, payment_status=paid,                  payments.status=paid
    admin rejects   → orders.status=pending,   payment_status=failed,                payments.status=rejected
                      (the customer may submit another UTR — a new payments row — or the order expires)
    no UTR in time  → orders.status=cancelled  (stock released), payments.status=expired

Money only moves between the customer's and the store's UPI apps, so nothing here can be verified
automatically: staff confirm each payment after seeing it in the bank/UPI app. Refunds are sent
back manually (history note "Refund due: …").
"""
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.core.clock import ensure_aware, utcnow
from app.core.exceptions import ConflictError, NotFoundError
from app.models.order import Order, OrderStatusHistory, Payment
from app.models.user import User
from app.services import loyalty_service, payment_service
from app.services.inventory_client import InventoryGateway
from app.services.payments.upi import (
    AWAITING_PAYMENT,
    CURRENCY,
    DEFAULT_PAYEE_NAME,
    EXPIRED,
    PAID,
    PROVIDER_CODE,
    REJECTED,
    SUBMITTED,
    build_upi_uri,
    format_amount,
    transaction_note,
)

logger = logging.getLogger(__name__)

AWAITING_VERIFICATION = "awaiting_verification"
# orders.payment_status values from which the customer may (re)submit a UTR.
SUBMITTABLE_PAYMENT_STATUSES = frozenset({"pending", AWAITING_VERIFICATION, "failed"})
# orders.payment_status values that the expiry sweep may cancel (no UTR pending verification).
EXPIRABLE_PAYMENT_STATUSES = frozenset({"pending", "failed"})

EXPIRED_NOTE = "UPI payment not received in time"
DEFAULT_REJECTION_REASON = "The payment could not be found in our UPI account."
NOT_UPI = "This order isn't paid by UPI."
NOT_AWAITING_VERIFICATION = "This order has no UPI payment awaiting verification."
DUPLICATE_UTR = (
    "This UPI transaction ID (UTR) has already been used for another payment. "
    "Please check the 12-digit UTR in your UPI app."
)


# --------------------------------------------------------------------------- helpers


def is_upi(order: Order) -> bool:
    return order.payment_method == PROVIDER_CODE


def _rows(order: Order) -> list[Payment]:
    return [row for row in order.payments if row.provider == PROVIDER_CODE]


def current_row(order: Order) -> Payment | None:
    """The latest UPI payments row (a resubmission after a rejection adds a new one)."""
    rows = _rows(order)
    return rows[-1] if rows else None


def _raw(row: Payment | None) -> dict[str, Any]:
    return dict(row.raw_response or {}) if row is not None else {}


def _set_raw(row: Payment, **changes: Any) -> None:
    row.raw_response = {**_raw(row), **changes}  # a NEW dict so SQLAlchemy notices the change


def _inr(amount: Decimal | float | str) -> str:
    return f"₹{Decimal(str(amount)):,.2f}"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _note(order: Order, note: str, changed_by: str) -> None:
    """A history row that records an event without changing the status."""
    order.status_history.append(
        OrderStatusHistory(from_status=order.status, to_status=order.status, note=note, changed_by=changed_by)
    )


def _lock(db: Session, order_id: uuid.UUID) -> Order:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items), selectinload(Order.status_history), selectinload(Order.payments))
        .with_for_update(of=Order)
        .execution_options(populate_existing=True)
    )
    order = db.scalars(stmt).one_or_none()
    if order is None:
        raise NotFoundError("We couldn't find that order.")
    return order


def deadline(order: Order, settings: Settings) -> datetime:
    """When an order without a UTR under verification expires.

    Counted from placement, or from the last rejection so the customer gets a fresh window to resubmit.
    """
    start = ensure_aware(order.placed_at) or utcnow()
    row = current_row(order)
    if row is not None and row.status == REJECTED:
        rejected_at = _parse_dt(_raw(row).get("rejected_at")) or ensure_aware(row.updated_at)
        if rejected_at is not None and rejected_at > start:
            start = rejected_at
    return start + timedelta(minutes=settings.upi_payment_expiry_minutes)


def can_submit(order: Order) -> bool:
    return is_upi(order) and order.status == "pending" and order.payment_status in SUBMITTABLE_PAYMENT_STATUSES


def _payee(order: Order, settings: Settings) -> tuple[str, str]:
    """The VPA / payee the customer was asked to pay (stored at placement), falling back to settings."""
    first = next(iter(_rows(order)), None)
    raw = _raw(first)
    vpa = str(raw.get("vpa") or settings.upi_vpa_clean)
    payee = str(raw.get("payee_name") or settings.upi_payee_name.strip() or DEFAULT_PAYEE_NAME)
    return vpa, payee


def checkout_payload(order: Order, settings: Settings) -> dict[str, Any]:
    """``payment`` block of a UPI checkout response. The amount is the server-side grand_total."""
    vpa, payee = _payee(order, settings)
    return {
        "provider": PROVIDER_CODE,
        "vpa": vpa,
        "payee_name": payee,
        "amount": float(format_amount(order.grand_total)),
        "currency": CURRENCY,
        "upi_uri": build_upi_uri(vpa, payee, order.grand_total, order.order_number),
        "note": transaction_note(order.order_number),
        "order_number": order.order_number,
        "expires_at": deadline(order, settings),
    }


def _state(order: Order, row: Payment | None) -> str:
    if order.payment_status == "refunded":
        return "refunded"
    if order.payment_status == "paid":
        return "paid"
    if order.payment_status == AWAITING_VERIFICATION:
        return "awaiting_verification"
    if order.status == "cancelled":
        return "expired" if row is not None and row.status == EXPIRED else "cancelled"
    if row is not None and row.status == REJECTED:
        return "rejected"
    return "awaiting_payment"


def payment_state(order: Order, settings: Settings) -> dict[str, Any] | None:
    """``OrderDetail.upi`` for a UPI order (None otherwise)."""
    if not is_upi(order):
        return None
    row = current_row(order)
    raw = _raw(row)
    submitted = row is not None and bool(row.provider_reference)
    return {
        **checkout_payload(order, settings),
        "state": _state(order, row),
        "can_submit": can_submit(order),
        "utr": row.provider_reference if submitted and row is not None else None,
        "payer_vpa": raw.get("payer_vpa") if submitted else None,
        "submitted_at": _parse_dt(raw.get("submitted_at")) if submitted else None,
        "rejection_reason": raw.get("rejection_reason") if row is not None and row.status == REJECTED else None,
    }


def payload_for_new_order(order: Order, settings: Settings) -> dict[str, Any] | None:
    if not (is_upi(order) and order.status == "pending" and order.payment_status == "pending"):
        return None
    return checkout_payload(order, settings)


# --------------------------------------------------------------------------- customer: submit the UTR


def _utr_taken(db: Session, utr: str, order_id: uuid.UUID) -> bool:
    """True when another order uses ``utr``, or this order has a non-rejected row with it."""
    rows = db.scalars(select(Payment).where(Payment.provider == PROVIDER_CODE, Payment.provider_reference == utr))
    return any(row.order_id != order_id or row.status != REJECTED for row in rows)


def submit_utr(
    db: Session,
    *,
    order_number: str,
    utr: str,
    payer_vpa: str | None,
    user: User | None,
    email: str | None,
    settings: Settings,
) -> Order:
    """Record the customer's UTR: payment_status → ``awaiting_verification`` (the order stays ``pending``).

    Owner, or guest with the order's email (404 otherwise). Allowed while the order is pending and
    unpaid: first submission, a correction while verification is pending, or after a rejection.
    Re-sending the same UTR is a no-op. A UTR used by any other UPI payment is a 409.
    """
    found = payment_service.get_accessible_order(db, order_number, user, email)
    if not is_upi(found):
        raise ConflictError(NOT_UPI)
    order = _lock(db, found.id)
    if order.payment_status in {"paid", "refunded"}:
        db.rollback()
        raise ConflictError("This order's payment has already been confirmed.")
    if order.status == "cancelled":
        db.rollback()
        raise ConflictError(
            "This order was cancelled, so the payment can't be submitted. "
            "If money was debited, contact us with the UTR and we'll send it back."
        )
    if not can_submit(order):
        db.rollback()
        raise ConflictError("This order is no longer awaiting a UPI payment.")

    row = current_row(order)
    if row is not None and row.status == SUBMITTED and row.provider_reference == utr:
        db.rollback()
        return order  # the same UTR again (double tap, retry): nothing to do
    if _utr_taken(db, utr, order.id):
        db.rollback()
        raise ConflictError(DUPLICATE_UTR, details=[{"field": "utr", "message": DUPLICATE_UTR}])

    now = utcnow()
    if row is None or row.status not in {AWAITING_PAYMENT, SUBMITTED}:
        # First UTR after a rejection: keep the rejected attempt and start a new row.
        vpa, payee = _payee(order, settings)
        row = Payment(
            provider=PROVIDER_CODE,
            provider_reference="",
            amount=order.grand_total,
            currency=CURRENCY,
            status=AWAITING_PAYMENT,
            raw_response={"method": PROVIDER_CODE, "vpa": vpa, "payee_name": payee},
        )
        order.payments.append(row)
    previous = row.provider_reference
    if previous:
        _set_raw(row, previous_utrs=[*(_raw(row).get("previous_utrs") or []), previous])
    row.provider_reference = utr
    row.status = SUBMITTED
    _set_raw(row, submitted_at=now.isoformat(), payer_vpa=payer_vpa or None)
    order.payment_status = AWAITING_VERIFICATION
    note = f"UPI payment submitted (UTR {utr})"
    if previous:
        note = f"{note}, replacing UTR {previous}"
    _note(order, note, "customer")
    try:
        db.commit()
    except IntegrityError as exc:  # uq_payments_upi_utr: the same UTR raced in on another order
        db.rollback()
        raise ConflictError(DUPLICATE_UTR, details=[{"field": "utr", "message": DUPLICATE_UTR}]) from exc
    logger.info("Order %s: UPI UTR submitted", order.order_number)
    return order


# --------------------------------------------------------------------------- staff: approve / reject


def verify_by_staff(db: Session, order_id: uuid.UUID, *, decision: str, note: str, changed_by: str) -> Order:
    """Approve or reject the submitted UPI payment of ``order_id``. Both are idempotent.

    approve: payment paid, payment_status ``paid``, ``pending → confirmed``. (If the customer had
             cancelled meanwhile the order stays cancelled and a "Refund due" note is written.)
    reject:  payment ``rejected``, payment_status ``failed``; the order stays ``pending`` so the
             customer can submit another UTR, or it expires.
    409 when the order is not a UPI order awaiting verification.
    """
    order = _lock(db, order_id)
    if not is_upi(order):
        db.rollback()
        raise ConflictError(NOT_UPI)
    row = current_row(order)
    now = utcnow().isoformat()
    reason = note.strip()

    if decision == "approve":
        if order.payment_status in {"paid", "refunded"} and row is not None and row.status in {PAID, "refunded"}:
            db.rollback()
            return order  # already approved
        if order.payment_status != AWAITING_VERIFICATION or row is None or row.status != SUBMITTED:
            db.rollback()
            raise ConflictError(NOT_AWAITING_VERIFICATION)
        row.status = PAID
        _set_raw(row, verified_at=now, verified_by=changed_by, verify_note=reason or None)
        order.payment_status = "paid"
        confirmed = f"UPI payment confirmed by {changed_by}" + (f": {reason}" if reason else "")
        if order.status == "pending":
            order.status_history.append(
                OrderStatusHistory(from_status="pending", to_status="confirmed", note=confirmed, changed_by=changed_by)
            )
            order.status = "confirmed"
        else:
            _note(order, f"{confirmed}. {refund_due_note(order)}", changed_by)
        db.commit()
        logger.info("Order %s: UPI payment approved by %s", order.order_number, changed_by)
        return order

    if row is not None and row.status == REJECTED and order.payment_status == "failed":
        db.rollback()
        return order  # already rejected
    if order.payment_status != AWAITING_VERIFICATION or row is None or row.status != SUBMITTED:
        db.rollback()
        raise ConflictError(NOT_AWAITING_VERIFICATION)
    reason = reason or DEFAULT_REJECTION_REASON
    row.status = REJECTED
    _set_raw(row, rejected_at=now, rejected_by=changed_by, rejection_reason=reason)
    order.payment_status = "failed"
    _note(order, f"UPI payment (UTR {row.provider_reference}) rejected by {changed_by}: {reason}", changed_by)
    db.commit()
    logger.info("Order %s: UPI payment rejected by %s", order.order_number, changed_by)
    return order


# --------------------------------------------------------------------------- cancellation / refunds


def refund_due_note(order: Order) -> str:
    return f"Refund due: send {_inr(order.grand_total)} back to the customer's UPI"


def note_for_cancellation(order: Order) -> str | None:
    """History note when a UPI order is cancelled. No automatic refund: the money is sent back by hand."""
    if not is_upi(order):
        return None
    if order.payment_status == "paid":
        return refund_due_note(order)
    if order.payment_status == AWAITING_VERIFICATION:
        row = current_row(order)
        utr = row.provider_reference if row is not None else ""
        return f"A UPI payment (UTR {utr}) was awaiting verification; if it was received, it must be refunded"
    return None


# --------------------------------------------------------------------------- expiry


def expire_unpaid_orders(
    db: Session,
    inventory: InventoryGateway,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Cancel UPI orders with no UTR awaiting verification after ``UPI_PAYMENT_EXPIRY_MINUTES``.

    Orders whose UTR is awaiting verification are never cancelled here. Each order is claimed with
    ``UPDATE … WHERE status='pending' AND payment_status IN ('pending','failed')`` so concurrent
    sweeps (several workers, the cron script) cancel it exactly once.
    """
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.upi_payment_expiry_minutes)
    candidates = list(
        db.scalars(
            select(Order)
            .where(
                Order.payment_method == PROVIDER_CODE,
                Order.status == "pending",
                Order.payment_status.in_(EXPIRABLE_PAYMENT_STATUSES),
                Order.placed_at < cutoff,
            )
            .options(selectinload(Order.payments))
            .order_by(Order.placed_at)
            .limit(limit)
        )
    )
    due = [order.id for order in candidates if deadline(order, settings) <= now]
    db.rollback()
    expired = 0
    for order_id in due:
        try:
            if _expire_one(db, order_id, inventory, now):
                expired += 1
        except Exception:  # noqa: BLE001 - one bad order must not stop the sweep
            db.rollback()
            logger.exception("Could not expire unpaid UPI order %s", order_id)
    if expired:
        logger.info("Expired %s unpaid UPI order(s)", expired)
    return expired


def _expire_one(db: Session, order_id: uuid.UUID, inventory: InventoryGateway, now: datetime) -> bool:
    claimed = db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "pending",
            Order.payment_status.in_(EXPIRABLE_PAYMENT_STATUSES),
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
    db.execute(
        update(Payment)
        .where(Payment.order_id == order_id, Payment.provider == PROVIDER_CODE, Payment.status == AWAITING_PAYMENT)
        .values(status=EXPIRED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.add(
        OrderStatusHistory(
            order_id=order_id, from_status="pending", to_status="cancelled", note=EXPIRED_NOTE, changed_by="system"
        )
    )
    loyalty_service.reverse_by_order_id(db, order_id, reason=EXPIRED_NOTE)
    db.commit()
    return True
