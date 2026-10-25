"""Order, coupon-redemption and purchase-history queries."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.order import CouponRedemption, Order, OrderItem


def _with_children(stmt):  # type: ignore[no-untyped-def]
    return stmt.options(
        selectinload(Order.items),
        selectinload(Order.status_history),
        selectinload(Order.payments),
    )


def get_by_number(db: Session, order_number: str) -> Order | None:
    return db.scalar(_with_children(select(Order).where(Order.order_number == order_number.strip().upper())))


def get_by_id(db: Session, order_id: uuid.UUID) -> Order | None:
    return db.scalar(_with_children(select(Order).where(Order.id == order_id)))


def order_number_exists(db: Session, order_number: str) -> bool:
    return (db.scalar(select(func.count(Order.id)).where(Order.order_number == order_number)) or 0) > 0


def list_for_user(db: Session, user_id: uuid.UUID, offset: int, limit: int) -> tuple[list[Order], int]:
    total = db.scalar(select(func.count(Order.id)).where(Order.user_id == user_id)) or 0
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .options(selectinload(Order.items))
        .order_by(Order.placed_at.desc(), Order.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(stmt)), int(total)


def count_redemptions(db: Session, coupon_id: uuid.UUID, user_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon_id)
    if user_id is not None:
        stmt = stmt.where(CouponRedemption.user_id == user_id)
    return db.scalar(stmt) or 0


def has_delivered_purchase(db: Session, user_id: uuid.UUID, product_id: uuid.UUID) -> bool:
    """True if the user has a delivered order containing the product (verified purchase)."""
    stmt = (
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user_id, Order.status == "delivered", OrderItem.product_id == product_id)
    )
    return (db.scalar(stmt) or 0) > 0
