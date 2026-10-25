"""Review queries."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.review import Review


def list_approved(db: Session, product_id: uuid.UUID, offset: int, limit: int) -> tuple[list[Review], int]:
    condition = (Review.product_id == product_id, Review.is_approved.is_(True))
    total = db.scalar(select(func.count(Review.id)).where(*condition)) or 0
    stmt = (
        select(Review)
        .where(*condition)
        .options(selectinload(Review.user))
        .order_by(Review.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(stmt)), int(total)


def rating_distribution(db: Session, product_id: uuid.UUID) -> dict[int, int]:
    stmt = (
        select(Review.rating, func.count(Review.id))
        .where(Review.product_id == product_id, Review.is_approved.is_(True))
        .group_by(Review.rating)
    )
    return {int(rating): int(count) for rating, count in db.execute(stmt).all()}


def exists_for_user(db: Session, product_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    stmt = select(func.count(Review.id)).where(Review.product_id == product_id, Review.user_id == user_id)
    return (db.scalar(stmt) or 0) > 0
