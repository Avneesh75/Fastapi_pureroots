"""User and address queries."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import Address, User


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def email_exists(db: Session, email: str) -> bool:
    return db.scalar(select(func.count(User.id)).where(User.email == email.strip().lower())) > 0


def list_addresses(db: Session, user_id: uuid.UUID) -> list[Address]:
    stmt = (
        select(Address)
        .where(Address.user_id == user_id)
        .order_by(Address.is_default.desc(), Address.created_at.desc())
    )
    return list(db.scalars(stmt))


def get_address(db: Session, user_id: uuid.UUID, address_id: uuid.UUID) -> Address | None:
    return db.scalar(select(Address).where(Address.id == address_id, Address.user_id == user_id))


def count_addresses(db: Session, user_id: uuid.UUID) -> int:
    return db.scalar(select(func.count(Address.id)).where(Address.user_id == user_id)) or 0
