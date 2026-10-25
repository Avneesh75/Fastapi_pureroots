"""Order placement, the status state machine and its inventory side effects (ARCHITECTURE §4.3, §5).

State machine::

    pending → confirmed | cancelled
    confirmed → processing | cancelled
    processing → packed | cancelled
    packed → shipped | cancelled
    shipped → out_for_delivery | delivered
    out_for_delivery → delivered
    delivered → refunded
    cancelled → refunded   (only when payment_status == paid)

Inventory: create → reserve; → cancelled → release; → shipped → commit; delivered → refunded → return.

Online (Razorpay) orders stay ``pending`` until the payment is verified (``payment_service``); a
paid Razorpay order moving to ``cancelled`` or ``refunded`` is refunded through Razorpay BEFORE the
status change is committed, and the change is refused if the refund fails.

Direct-UPI orders also stay ``pending`` until staff confirm the payment (``upi_service``); staff can't
confirm an unpaid UPI order through a status change. Cancelling a paid UPI order is allowed and
records "Refund due: …" (the money is sent back by hand; ``cancelled → refunded`` marks it done).

PureRoots Coins (``loyalty_service``): placement writes ``redeem`` + ``earn_pending``; → delivered confirms
the earn; → cancelled / refunded returns the coins used and reverses the earn. All idempotent.
"""
import logging
import secrets
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.clock import utcnow
from app.core.constants import ORDER_STATUS_LABELS, ORDER_STATUSES, PAYMENT_STATUS_LABELS
from app.core.exceptions import (
    ConflictError,
    CouponNotCombinableError,
    InsufficientStockError,
    NotFoundError,
    ValidationAppError,
)
from app.models.order import CouponRedemption, Order, OrderItem, OrderStatusHistory, Payment
from app.models.user import User
from app.repositories import catalog_repository, order_repository, user_repository
from app.schemas.address import AddressCreate
from app.schemas.order import (
    CheckoutRequest,
    OrderDetail,
    OrderItemOut,
    OrderSummary,
    StatusHistoryOut,
    TimelineStep,
)
from app.services import address_service, cart_service, loyalty_service, payment_service, upi_service
from app.services.cart_service import CartContext
from app.services.catalog_service import image_url_for
from app.services.inventory_client import InventoryGateway
from app.services.payments import registry
from app.services.pricing_service import money, q2

logger = logging.getLogger(__name__)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"processing", "cancelled"}),
    "processing": frozenset({"packed", "cancelled"}),
    "packed": frozenset({"shipped", "cancelled"}),
    "shipped": frozenset({"out_for_delivery", "delivered"}),
    "out_for_delivery": frozenset({"delivered"}),
    "delivered": frozenset({"refunded"}),
    "cancelled": frozenset({"refunded"}),
    "refunded": frozenset(),
}
CUSTOMER_CANCELLABLE = frozenset({"pending", "confirmed", "processing"})
TIMELINE_PATH = ("pending", "confirmed", "processing", "packed", "shipped", "out_for_delivery", "delivered")

ORDER_NUMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
ORDER_NOT_FOUND = "We couldn't find that order."
LOOKUP_NOT_FOUND = "We couldn't find an order with those details."
EMPTY_CART = "Your cart is empty."


# --------------------------------------------------------------------------- state machine


def status_label(status: str) -> str:
    return ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title())


def payment_status_label(payment_status: str) -> str:
    return PAYMENT_STATUS_LABELS.get(payment_status, (payment_status or "").replace("_", " ").capitalize())


def can_transition(from_status: str, to_status: str, payment_status: str) -> bool:
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        return False
    if from_status == "cancelled" and to_status == "refunded":
        return payment_status == "paid"
    return True


def can_customer_cancel(order: Order) -> bool:
    return order.status in CUSTOMER_CANCELLABLE


def _record_transition(order: Order, to_status: str, note: str, changed_by: str) -> None:
    order.status_history.append(
        OrderStatusHistory(from_status=order.status, to_status=to_status, note=note or "", changed_by=changed_by)
    )
    order.status = to_status


def _apply_inventory_effect(inventory: InventoryGateway, order: Order, from_status: str, to_status: str) -> None:
    if to_status == "cancelled":
        inventory.release(order.id)
    elif to_status == "shipped":
        inventory.commit(order.id)
    elif to_status == "refunded" and from_status == "delivered":
        inventory.return_stock(order.id)


def _apply_payment_effect(order: Order, to_status: str) -> None:
    if to_status == "delivered" and order.payment_method == "cod":
        order.payment_status = "paid"
        for payment in order.payments:
            payment.status = "paid"
    elif to_status == "refunded" and order.payment_status == "paid":
        order.payment_status = "refunded"
        for payment in order.payments:
            # UPI orders can have rejected/expired attempts next to the paid one: only the paid one was refunded.
            if not upi_service.is_upi(order) or payment.status == "paid":
                payment.status = "refunded"


def change_status(
    db: Session,
    order: Order,
    to_status: str,
    *,
    note: str,
    changed_by: str,
    inventory: InventoryGateway,
) -> Order:
    """Validate and apply a transition: inventory call first, then history + status in one commit."""
    target = (to_status or "").strip().lower()
    if target not in ORDER_STATUSES:
        message = "Unknown order status."
        raise ValidationAppError(message, details=[{"field": "status", "message": message}])
    from_status = order.status
    if (
        from_status == "pending"
        and target == "confirmed"
        and payment_service.is_online(order)
        and order.payment_status != "paid"
    ):
        raise ConflictError(
            "This order is waiting for its online payment and can't be confirmed until it is paid.",
            details=[{"from_status": from_status, "to_status": target, "payment_status": order.payment_status}],
        )
    if from_status == "pending" and target == "confirmed" and upi_service.is_upi(order) and order.payment_status != "paid":
        raise ConflictError(
            "This UPI order can't be confirmed until its payment is verified. Check the UTR in your bank or "
            "UPI app, then use \"Confirm payment received\".",
            details=[{"from_status": from_status, "to_status": target, "payment_status": order.payment_status}],
        )
    if not can_transition(from_status, target, order.payment_status):
        raise ConflictError(
            f"An order that is {status_label(from_status).lower()} cannot be changed to "
            f"{status_label(target).lower()}.",
            details=[
                {
                    "from_status": from_status,
                    "to_status": target,
                    "allowed": sorted(ALLOWED_TRANSITIONS.get(from_status, frozenset())),
                }
            ],
        )

    # Money first: a failed refund raises here and nothing has changed yet.
    refund_note = payment_service.refund_for_transition(order, target)
    if refund_note:
        note = f"{note}. {refund_note}" if note else refund_note
    upi_note = upi_service.note_for_cancellation(order) if target == "cancelled" else None
    if upi_note:
        note = f"{note}. {upi_note}" if note else upi_note
    try:
        _apply_inventory_effect(inventory, order, from_status, target)
    except Exception:
        if refund_note:
            # The refund went through: keep that fact even though the status change failed.
            order.status_history.append(
                OrderStatusHistory(
                    from_status=from_status,
                    to_status=from_status,
                    note=f"{refund_note}; the status change to {target} failed and must be retried",
                    changed_by=changed_by,
                )
            )
            db.commit()
        raise
    _record_transition(order, target, note, changed_by)
    _apply_payment_effect(order, target)
    loyalty_service.on_status_change(db, order, target, changed_by=changed_by)
    db.commit()
    logger.info("Order %s: %s -> %s by %s", order.order_number, from_status, target, changed_by)
    return order


# --------------------------------------------------------------------------- placement


def generate_order_number(now: datetime | None = None) -> str:
    """``PR-YYMMDD-XXXXX``."""
    stamp = (now or utcnow()).strftime("%y%m%d")
    suffix = "".join(secrets.choice(ORDER_NUMBER_ALPHABET) for _ in range(5))
    return f"PR-{stamp}-{suffix}"


def _unique_order_number(db: Session, now: datetime) -> str:
    for _ in range(10):
        candidate = generate_order_number(now)
        if not order_repository.order_number_exists(db, candidate):
            return candidate
    raise ConflictError("Could not allocate an order number. Please try again.")


def _shipping_snapshot(db: Session, user: User | None, payload: CheckoutRequest) -> dict[str, str]:
    if payload.address_id is not None:
        if user is None:
            message = "Sign in to use a saved address."
            raise ValidationAppError(message, details=[{"field": "address_id", "message": message}])
        address = user_repository.get_address(db, user.id, payload.address_id)
        if address is None:
            message = "We couldn't find that saved address."
            raise ValidationAppError(message, details=[{"field": "address_id", "message": message}])
        return address.snapshot()
    assert payload.shipping_address is not None  # guaranteed by schema validation
    return payload.shipping_address.model_dump()


def _stock_details(lines: list[cart_service.CartLine]) -> list[dict[str, Any]]:
    return [
        {
            "variant_id": str(line.variant.id),
            "sku": line.variant.sku,
            "requested": line.item.quantity,
            "available": line.available,
        }
        for line in lines
    ]


def _reservation_items(order: Order) -> list[dict[str, Any]]:
    quantities: dict[uuid.UUID, int] = defaultdict(int)
    for item in order.items:
        quantities[item.variant_id] += item.quantity
    return [{"variant_id": str(variant_id), "quantity": quantity} for variant_id, quantity in quantities.items()]


def _safe_release(inventory: InventoryGateway, order_id: uuid.UUID, order_number: str) -> None:
    try:
        inventory.release(order_id)
    except Exception:  # noqa: BLE001 - compensation must never mask the original error
        logger.exception("Compensating release failed for order %s", order_number)


def place_order(
    db: Session,
    ctx: CartContext,
    payload: CheckoutRequest,
    inventory: InventoryGateway,
    settings: Settings,
) -> Order:
    """Checkout flow (§5): re-price → create pending order → reserve → pay → confirm → clear cart.

    Online providers (Razorpay) create their payment order in ``initiate``; the order then stays
    ``pending`` until the payment is verified. If ``initiate`` fails, stock is released and the
    whole order is rolled back.
    """
    cart = cart_service.find_cart(db, ctx)
    if cart is None or not cart.items:
        raise ValidationAppError(EMPTY_CART)

    if ctx.user is not None and cart.use_coins:
        # Lock the customer row before pricing so the coins redeemed below come from a balance no
        # concurrent checkout can spend at the same time (held until this transaction ends).
        loyalty_service.lock_user(db, ctx.user.id)
    priced = cart_service.price(db, ctx, cart, payload.delivery_method, settings)
    if priced.out_of_stock:
        raise InsufficientStockError(
            "Some items in your cart are out of stock. Please review your cart.",
            details=_stock_details(priced.out_of_stock),
        )
    if priced.quantities_adjusted:
        raise InsufficientStockError(
            "Some quantities in your cart changed because of limited stock. Please review your cart.",
            details=_stock_details(priced.lines),
        )
    if not priced.purchasable:
        raise ValidationAppError(EMPTY_CART)
    if priced.coupon_check is not None and priced.coupon_check.not_combinable:
        # The re-price already dropped the coupon; tell the customer before charging a different total.
        message = CouponNotCombinableError.default_message
        raise CouponNotCombinableError(message, details=[{"field": "coupon_code", "message": message}])
    if priced.coupon_check is not None and not priced.coupon_check.is_valid:
        message = priced.coupon_check.error or "This coupon can no longer be applied."
        raise ValidationAppError(message, details=[{"field": "coupon_code", "message": message}])

    provider = registry.get_provider(payload.payment_method, settings)
    if priced.breakdown.total <= 0 and provider.code != "cod":
        message = "Your coins cover this whole order, so there is nothing to pay online. Choose Cash on Delivery."
        raise ValidationAppError(message, details=[{"field": "payment_method", "message": message}])
    user = ctx.user
    shipping_address = _shipping_snapshot(db, user, payload)
    now = utcnow()
    breakdown = priced.breakdown
    coupon = priced.coupon_check.coupon if priced.coupon_check and priced.coupon_check.is_valid else None

    order_id = uuid.uuid4()
    order_number = _unique_order_number(db, now)
    order = Order(
        id=order_id,
        order_number=order_number,
        user_id=user.id if user else None,
        status="pending",
        payment_status="pending",
        payment_method=provider.code,
        delivery_method=payload.delivery_method,
        customer_name=payload.customer.full_name,
        customer_email=payload.customer.email,
        customer_phone=payload.customer.phone,
        shipping_address=shipping_address,
        subtotal=breakdown.subtotal,
        discount_total=breakdown.coupon_discount,
        offer_discount_total=breakdown.offer_discount,
        shipping_fee=breakdown.shipping,
        tax_total=breakdown.tax_included,
        grand_total=breakdown.total,
        coupon_code=coupon.code.upper() if coupon is not None else None,
        coins_redeemed=priced.coins.redeemed if user is not None else 0,
        coins_earned=priced.coins.to_earn if user is not None else 0,
        notes=payload.notes,
        placed_at=now,
    )
    for line in priced.purchasable:
        offer_line = priced.offer_line(line)
        assert offer_line is not None  # every purchasable line is priced by price_lines
        offer = offer_line.offer
        order.items.append(
            OrderItem(
                product_id=line.product.id,
                variant_id=line.variant.id,
                product_name=line.product.name,
                product_slug=line.product.slug,
                variant_label=line.variant.label,
                sku=line.variant.sku,
                unit_mrp=q2(line.variant.mrp),
                unit_original_price=q2(offer_line.unit_original),
                unit_price=q2(offer_line.unit_final),
                quantity=line.item.quantity,
                tax_rate=line.product.tax_rate or 0,
                line_total=q2(offer_line.line_final),
                offer_discount=q2(offer_line.line_discount),
                offer_id=uuid.UUID(str(offer.id)) if offer is not None else None,
                offer_name=offer.name[:120] if offer is not None else None,
                offer_label=offer_line.label if offer is not None else None,
            )
        )
    order.status_history.append(
        OrderStatusHistory(from_status=None, to_status="pending", note="Order placed", changed_by="customer")
    )
    db.add(order)
    # CouponRedemption has an FK to orders but no ORM relationship, so the unit of
    # work cannot infer insert order — flush the order row first (PostgreSQL enforces it).
    db.flush()
    if coupon is not None and breakdown.coupon_discount > 0:
        db.add(
            CouponRedemption(
                coupon_id=coupon.id,
                coupon_code=coupon.code.upper(),
                user_id=user.id if user else None,
                order_id=order_id,
                discount_amount=breakdown.coupon_discount,
            )
        )
    if user is not None:
        # Re-checks the locked balance (409 if it no longer covers the coins); rolled back with the order on failure.
        try:
            loyalty_service.redeem_for_order(db, user.id, order, order.coins_redeemed)
            loyalty_service.earn_pending_for_order(db, user.id, order, order.coins_earned)
        except Exception:
            db.rollback()
            raise
    if payload.save_address and user is not None and payload.address_id is None and payload.shipping_address:
        try:
            address_service.create_address(
                db, user, AddressCreate(**payload.shipping_address.model_dump()), commit=False
            )
        except ValidationAppError:
            logger.info("Address book full for user %s; order placed without saving the address", user.id)

    # Step 3: reserve stock. On failure nothing is persisted.
    try:
        db.flush()
        inventory.reserve(order_id, order_number, _reservation_items(order))
    except Exception:
        db.rollback()
        raise

    # Steps 4-6: payment, confirm, clear cart. Any failure releases the reservation.
    try:
        initiation = provider.initiate(order)
        order.payments.append(
            Payment(
                provider=provider.code,
                provider_reference=initiation.reference,
                amount=breakdown.total,
                currency="INR",
                status=initiation.payment_status,
                raw_response=initiation.raw_response,
            )
        )
        order.payment_status = initiation.order_payment_status
        if getattr(provider, "confirms_on_placement", True):
            _record_transition(order, "confirmed", "Order confirmed", "system")
        # Online payments: the order stays pending until payment_service marks it paid.
        cart_service.clear_cart(cart)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Order %s failed after stock reservation; releasing stock", order_number)
        _safe_release(inventory, order_id, order_number)
        raise

    logger.info("Order %s placed (%s items, total %s)", order_number, len(priced.purchasable), breakdown.total)
    placed = order_repository.get_by_id(db, order_id)
    assert placed is not None
    return placed


# --------------------------------------------------------------------------- queries


def get_user_order(db: Session, user: User, order_number: str) -> Order:
    order = order_repository.get_by_number(db, order_number)
    if order is None or order.user_id != user.id:
        raise NotFoundError(ORDER_NOT_FOUND)
    return order


def get_order_by_id(db: Session, order_id: uuid.UUID) -> Order:
    order = order_repository.get_by_id(db, order_id)
    if order is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    return order


def lookup_order(db: Session, order_number: str, email: str) -> Order:
    order = order_repository.get_by_number(db, order_number)
    if order is None or order.customer_email.lower() != email.strip().lower():
        raise NotFoundError(LOOKUP_NOT_FOUND)
    return order


def list_user_orders(db: Session, user: User, offset: int, limit: int) -> tuple[list[Order], int]:
    return order_repository.list_for_user(db, user.id, offset, limit)


def cancel_by_customer(db: Session, user: User, order_number: str, inventory: InventoryGateway) -> Order:
    order = get_user_order(db, user, order_number)
    if not can_customer_cancel(order):
        raise ConflictError(
            f"This order is already {status_label(order.status).lower()} and can no longer be cancelled."
        )
    return change_status(db, order, "cancelled", note="Cancelled by customer", changed_by="customer", inventory=inventory)


# --------------------------------------------------------------------------- read models


def build_timeline(order: Order) -> list[TimelineStep]:
    reached_at: dict[str, datetime] = {}
    for entry in sorted(order.status_history, key=lambda history: history.created_at):
        reached_at.setdefault(entry.to_status, entry.created_at)

    if order.status in TIMELINE_PATH:
        current = TIMELINE_PATH.index(order.status)
        return [
            TimelineStep(status=step, label=status_label(step), reached=index <= current, at=reached_at.get(step))
            for index, step in enumerate(TIMELINE_PATH)
        ]

    steps = [
        TimelineStep(status=step, label=status_label(step), reached=True, at=reached_at.get(step))
        for step in TIMELINE_PATH
        if step in reached_at
    ]
    for terminal in ("cancelled", "refunded"):
        if terminal in reached_at or order.status == terminal:
            steps.append(
                TimelineStep(status=terminal, label=status_label(terminal), reached=True, at=reached_at.get(terminal))
            )
    return steps


def _image_map(db: Session, orders: list[Order]) -> dict[uuid.UUID, str]:
    product_ids = {item.product_id for order in orders for item in order.items}
    return catalog_repository.first_image_paths(db, product_ids)


def build_order_detail(db: Session, order: Order, settings: Settings) -> OrderDetail:
    images = _image_map(db, [order])
    items = [
        OrderItemOut(
            id=item.id,
            product_id=item.product_id,
            variant_id=item.variant_id,
            product_name=item.product_name,
            product_slug=item.product_slug,
            variant_label=item.variant_label,
            sku=item.sku,
            image_url=image_url_for(images.get(item.product_id), item.product_slug, settings),
            unit_mrp=money(item.unit_mrp),
            unit_original_price=money(item.unit_original_price),
            unit_price=money(item.unit_price),
            quantity=item.quantity,
            tax_rate=float(item.tax_rate or 0),
            line_total=money(item.line_total),
            offer_discount=money(item.offer_discount),
            offer_id=item.offer_id,
            offer_name=item.offer_name,
            offer_label=item.offer_label,
        )
        for item in order.items
    ]
    history = [
        StatusHistoryOut(
            from_status=entry.from_status,
            to_status=entry.to_status,
            note=entry.note or "",
            changed_by=entry.changed_by,
            created_at=entry.created_at,
        )
        for entry in sorted(order.status_history, key=lambda history: history.created_at)
    ]
    return OrderDetail(
        id=order.id,
        order_number=order.order_number,
        status=order.status,
        status_label=status_label(order.status),
        payment_status=order.payment_status,
        payment_status_label=payment_status_label(order.payment_status),
        payment_method=order.payment_method,
        delivery_method=order.delivery_method,
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        customer_phone=order.customer_phone,
        shipping_address=dict(order.shipping_address or {}),
        subtotal=money(order.subtotal),
        discount_total=money(order.discount_total),
        offer_discount_total=money(order.offer_discount_total),
        items_total=money(q2(order.subtotal) + q2(order.offer_discount_total)),
        shipping_fee=money(order.shipping_fee),
        tax_total=money(order.tax_total),
        grand_total=money(order.grand_total),
        coupon_code=order.coupon_code,
        coins_redeemed=order.coins_redeemed or 0,
        coins_earned=order.coins_earned or 0,
        coins_status=loyalty_service.coins_status(order),
        notes=order.notes or "",
        item_count=order.item_count,
        placed_at=order.placed_at,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=items,
        status_history=history,
        can_cancel=can_customer_cancel(order),
        can_pay=payment_service.can_pay(order),
        timeline=build_timeline(order),
        upi=upi_service.payment_state(order, settings),
    )


def build_order_summaries(db: Session, orders: list[Order], settings: Settings) -> list[OrderSummary]:
    images = _image_map(db, orders)
    summaries = []
    for order in orders:
        first = order.items[0] if order.items else None
        summaries.append(
            OrderSummary(
                order_number=order.order_number,
                status=order.status,
                status_label=status_label(order.status),
                payment_status=order.payment_status,
                payment_status_label=payment_status_label(order.payment_status),
                grand_total=money(order.grand_total),
                offer_discount_total=money(order.offer_discount_total),
                item_count=order.item_count,
                placed_at=order.placed_at,
                first_item_image=(
                    image_url_for(images.get(first.product_id), first.product_slug, settings) if first else None
                ),
                coins_redeemed=order.coins_redeemed or 0,
                coins_earned=order.coins_earned or 0,
                coins_status=loyalty_service.coins_status(order),
            )
        )
    return summaries
