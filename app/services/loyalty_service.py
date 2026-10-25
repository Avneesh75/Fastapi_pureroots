"""PureRoots Coins: the ledger, cached balances and the order hooks. Rule maths live in ``loyalty_rules``.

Every balance change writes one ``loyalty_transactions`` row and updates ``users.coins_available`` /
``users.coins_pending`` in the same transaction, after locking the user row (``SELECT … FOR UPDATE`` on
PostgreSQL; SQLite ignores the lock, and tests run single-threaded). Callers commit.

Order hooks (all idempotent — they look at the ledger rows already written for the order):

* placement            → ``redeem`` (−coins used) and ``earn_pending`` (+coins promised)
* → delivered          → ``earn_confirmed`` (pending → available)
* → cancelled/refunded → ``redeem_reversed`` (coins used come back) and ``earn_reversed`` (from pending, or from
  available when already credited — which may leave a negative available balance if they were spent; the
  ledger note says so and redemption stays blocked until the balance is positive again)
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.models.user import User
from app.schemas.loyalty import LoyaltyRuleOut, LoyaltySummaryOut, LoyaltyTransactionOut
from app.services.loyalty_rules import LoyaltyConfig, rule_text

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "earn_pending": "Earned - pending delivery",
    "earn_confirmed": "Coins credited",
    "earn_reversed": "Earned coins reversed",
    "redeem": "Used on an order",
    "redeem_reversed": "Coins returned",
    "adjustment": "Adjustment by PureRoots",
}
REVERSING_STATUSES = frozenset({"cancelled", "refunded"})
BALANCE_CHANGED = (
    "Your coin balance changed while you were checking out. Please review your cart and place the order again."
)
NEGATIVE_NOTE = (
    "{spent} of these coins had already been spent, so the available balance is now negative; "
    "coins can't be used until it is positive again."
)


def config(settings: Settings) -> LoyaltyConfig:
    return LoyaltyConfig.from_settings(settings)


# --------------------------------------------------------------------------- ledger primitives


def lock_user(db: Session, user_id: uuid.UUID) -> User | None:
    """Load the user row with ``FOR UPDATE`` (fresh values, held until commit/rollback)."""
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    return db.scalars(stmt).one_or_none()


def _next_timestamp(db: Session, user_id: uuid.UUID) -> datetime:
    """A timestamp strictly after the user's latest ledger row.

    Several rows are often written in one transaction (e.g. ``redeem`` + ``earn_pending`` at placement),
    and PostgreSQL's ``now()`` is fixed per transaction, so equal timestamps would make the history order
    random. Keeping them strictly increasing preserves the order the rows were written in.
    """
    now = datetime.now(timezone.utc)
    latest = db.scalar(select(func.max(LoyaltyTransaction.created_at)).where(LoyaltyTransaction.user_id == user_id))
    if latest is not None:
        if latest.tzinfo is None:  # SQLite returns naive datetimes
            latest = latest.replace(tzinfo=timezone.utc)
        if latest >= now:
            return latest + timedelta(microseconds=1)
    return now


def _record(
    db: Session,
    user_id: uuid.UUID,
    kind: str,
    coins: int,
    *,
    available_delta: int = 0,
    pending_delta: int = 0,
    order_id: uuid.UUID | None = None,
    note: str = "",
    created_by: str = "system",
) -> LoyaltyTransaction | None:
    user = lock_user(db, user_id)
    if user is None:
        return None
    user.coins_available = (user.coins_available or 0) + available_delta
    user.coins_pending = (user.coins_pending or 0) + pending_delta
    row = LoyaltyTransaction(
        created_at=_next_timestamp(db, user_id),
        user_id=user_id,
        order_id=order_id,
        kind=kind,
        coins=coins,
        available_after=user.coins_available,
        pending_after=user.coins_pending,
        note=note[:1000],
        created_by=created_by[:150],
    )
    db.add(row)
    db.flush()  # sessions don't autoflush: make the row and balances visible to the next lookup
    return row


def _order_rows(db: Session, order_id: uuid.UUID) -> list[LoyaltyTransaction]:
    return list(
        db.scalars(
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.order_id == order_id)
            .order_by(LoyaltyTransaction.created_at)
        )
    )


def _first(rows: list[LoyaltyTransaction], kind: str) -> LoyaltyTransaction | None:
    return next((row for row in rows if row.kind == kind), None)


# --------------------------------------------------------------------------- placement


def redeem_for_order(db: Session, user_id: uuid.UUID, order: Order, coins: int) -> None:
    """Deduct ``coins`` for ``order``. Race-safe: re-checks the locked balance (409 if it no longer covers it)."""
    if coins <= 0:
        return
    user = lock_user(db, user_id)
    if user is None or (user.coins_available or 0) < coins:
        raise ConflictError(BALANCE_CHANGED, details=[{"field": "coins", "message": BALANCE_CHANGED}])
    _record(
        db,
        user_id,
        "redeem",
        -coins,
        available_delta=-coins,
        order_id=order.id,
        note=f"Used on order {order.order_number}",
        created_by="customer",
    )


def earn_pending_for_order(db: Session, user_id: uuid.UUID, order: Order, coins: int, *, note: str = "") -> None:
    if coins <= 0:
        return
    _record(
        db,
        user_id,
        "earn_pending",
        coins,
        pending_delta=coins,
        order_id=order.id,
        note=note or f"Earned on order {order.order_number} - credited on delivery",
    )


# --------------------------------------------------------------------------- order hooks


def confirm_for_order(db: Session, order: Order, *, created_by: str = "system") -> bool:
    """Delivered: pending → available. Idempotent."""
    if order.user_id is None:
        return False
    rows = _order_rows(db, order.id)
    pending = _first(rows, "earn_pending")
    if pending is None or _first(rows, "earn_confirmed") or _first(rows, "earn_reversed"):
        return False
    coins = pending.coins
    _record(
        db,
        order.user_id,
        "earn_confirmed",
        coins,
        available_delta=coins,
        pending_delta=-coins,
        order_id=order.id,
        note=f"Order {order.order_number} delivered",
        created_by=created_by,
    )
    return True


def reverse_for_order(db: Session, order: Order, *, reason: str = "", created_by: str = "system") -> bool:
    """Cancelled / refunded / expired: give back coins used, take back coins earned. Idempotent."""
    if order.user_id is None:
        return False
    rows = _order_rows(db, order.id)
    changed = False
    why = reason or f"Order {order.order_number} {order.status}"

    redeem = _first(rows, "redeem")
    if redeem is not None and _first(rows, "redeem_reversed") is None:
        coins = -redeem.coins
        _record(
            db,
            order.user_id,
            "redeem_reversed",
            coins,
            available_delta=coins,
            order_id=order.id,
            note=f"{why}: coins used on it returned",
            created_by=created_by,
        )
        changed = True

    earned = _first(rows, "earn_pending")
    if earned is not None and _first(rows, "earn_reversed") is None:
        coins = earned.coins
        if _first(rows, "earn_confirmed") is not None:
            user = lock_user(db, order.user_id)
            before = (user.coins_available or 0) if user is not None else 0
            note = f"{why}: credited coins reversed"
            if before < coins:
                note = f"{note}. " + NEGATIVE_NOTE.format(spent=coins - max(before, 0))
            _record(
                db, order.user_id, "earn_reversed", -coins, available_delta=-coins,
                order_id=order.id, note=note, created_by=created_by,
            )
        else:
            _record(
                db, order.user_id, "earn_reversed", -coins, pending_delta=-coins,
                order_id=order.id, note=f"{why}: pending coins cancelled", created_by=created_by,
            )
        changed = True
    return changed


def on_status_change(db: Session, order: Order, to_status: str, *, changed_by: str = "system") -> None:
    """Called by ``order_service.change_status`` before its commit."""
    if to_status == "delivered":
        confirm_for_order(db, order, created_by=changed_by)
    elif to_status in REVERSING_STATUSES:
        reverse_for_order(db, order, reason=f"Order {order.order_number} {to_status}", created_by=changed_by)


def reverse_by_order_id(db: Session, order_id: uuid.UUID, *, reason: str) -> None:
    """Expiry sweeps (which cancel with a bulk UPDATE) call this before their commit."""
    order = db.get(Order, order_id)
    if order is not None:
        reverse_for_order(db, order, reason=reason)


def coins_status(order: Order) -> str | None:
    """``pending`` | ``credited`` | ``reversed`` (None when the order earns nothing)."""
    if not order.coins_earned:
        return None
    if order.status in REVERSING_STATUSES:
        return "reversed"
    if order.status == "delivered":
        return "credited"
    return "pending"


# --------------------------------------------------------------------------- admin adjustment


@dataclass
class AdjustmentResult:
    user: User
    transaction: LoyaltyTransaction


def adjust(db: Session, user_id: uuid.UUID, coins: int, note: str, changed_by: str) -> AdjustmentResult:
    user = lock_user(db, user_id)
    if user is None:
        raise NotFoundError("We couldn't find that customer.")
    if coins < 0 and (user.coins_available or 0) + coins < 0:
        db.rollback()
        message = f"The customer has only {max(user.coins_available or 0, 0)} coins available to deduct."
        raise ConflictError(message, details=[{"field": "coins", "message": message}])
    row = _record(db, user_id, "adjustment", coins, available_delta=coins, note=note, created_by=changed_by)
    assert row is not None
    db.commit()
    db.refresh(user)
    logger.info("Coins adjusted for user %s by %s: %+d", user_id, changed_by, coins)
    return AdjustmentResult(user=user, transaction=row)


# --------------------------------------------------------------------------- read models


def transaction_out(row: LoyaltyTransaction, order_number: str | None = None) -> LoyaltyTransactionOut:
    return LoyaltyTransactionOut(
        id=row.id,
        kind=row.kind,
        kind_label=KIND_LABELS.get(row.kind, row.kind.replace("_", " ").capitalize()),
        coins=row.coins,
        available_after=row.available_after,
        pending_after=row.pending_after,
        note=row.note or "",
        order_number=order_number,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def rule_out(settings: Settings) -> LoyaltyRuleOut:
    cfg = config(settings)
    return LoyaltyRuleOut(
        enabled=cfg.enabled,
        earn_percent=cfg.earn_percent,
        step_amount=float(cfg.step_amount),
        max_steps=cfg.max_steps,
        text=rule_text(cfg),
    )


def lifetime_earned(db: Session, user_id: uuid.UUID) -> int:
    """Coins credited on delivery (minus credited coins later reversed) plus positive adjustments."""
    rows = db.execute(
        select(LoyaltyTransaction.kind, LoyaltyTransaction.order_id, LoyaltyTransaction.coins).where(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.kind.in_(("earn_confirmed", "earn_reversed", "adjustment")),
        )
    ).all()
    confirmed_orders = {order_id for kind, order_id, _ in rows if kind == "earn_confirmed"}
    total = 0
    for kind, order_id, coins in rows:
        if kind == "earn_confirmed" or (kind == "adjustment" and coins > 0):
            total += coins
        elif kind == "earn_reversed" and order_id in confirmed_orders:
            total += coins  # negative
    return max(total, 0)


def summary(db: Session, user: User, settings: Settings, offset: int, limit: int) -> tuple[LoyaltySummaryOut, int]:
    total = db.scalar(select(func.count(LoyaltyTransaction.id)).where(LoyaltyTransaction.user_id == user.id)) or 0
    rows = db.execute(
        select(LoyaltyTransaction, Order.order_number)
        .outerjoin(Order, Order.id == LoyaltyTransaction.order_id)
        .where(LoyaltyTransaction.user_id == user.id)
        .order_by(LoyaltyTransaction.created_at.desc(), LoyaltyTransaction.id)
        .offset(offset)
        .limit(limit)
    ).all()
    data = LoyaltySummaryOut(
        available=user.coins_available or 0,
        pending=user.coins_pending or 0,
        lifetime_earned=lifetime_earned(db, user.id),
        rule=rule_out(settings),
        transactions=[transaction_out(row, number) for row, number in rows],
    )
    return data, total


def adjustment_payload(result: AdjustmentResult, order_number: str | None = None) -> dict[str, Any]:
    return {
        "user_id": result.user.id,
        "available": result.user.coins_available,
        "pending": result.user.coins_pending,
        "transaction": transaction_out(result.transaction, order_number),
    }
