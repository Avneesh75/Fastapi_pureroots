"""Saved addresses for signed-in customers."""
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationAppError
from app.models.user import Address, User
from app.repositories import user_repository
from app.schemas.address import AddressCreate, AddressUpdate

MAX_ADDRESSES = 20
NOT_FOUND = "We couldn't find that address."


def _clear_default(db: Session, user_id: uuid.UUID, keep_id: uuid.UUID | None = None) -> None:
    stmt = update(Address).where(Address.user_id == user_id, Address.is_default.is_(True))
    if keep_id is not None:
        stmt = stmt.where(Address.id != keep_id)
    db.execute(stmt.values(is_default=False))


def list_addresses(db: Session, user: User) -> list[Address]:
    return user_repository.list_addresses(db, user.id)


def get_address(db: Session, user: User, address_id: uuid.UUID) -> Address:
    address = user_repository.get_address(db, user.id, address_id)
    if address is None:
        raise NotFoundError(NOT_FOUND)
    return address


def create_address(db: Session, user: User, data: AddressCreate, *, commit: bool = True) -> Address:
    existing = user_repository.count_addresses(db, user.id)
    if existing >= MAX_ADDRESSES:
        raise ValidationAppError(f"You can save up to {MAX_ADDRESSES} addresses.")
    make_default = data.is_default or existing == 0
    if make_default:
        _clear_default(db, user.id)
    address = Address(user_id=user.id, **data.model_dump(exclude={"is_default"}), is_default=make_default)
    db.add(address)
    if commit:
        db.commit()
        db.refresh(address)
    else:
        db.flush()
    return address


def update_address(db: Session, user: User, address_id: uuid.UUID, data: AddressUpdate) -> Address:
    address = get_address(db, user, address_id)
    changes = {key: value for key, value in data.model_dump(exclude_unset=True).items() if value is not None}
    if changes.get("is_default"):
        _clear_default(db, user.id, keep_id=address.id)
    for key, value in changes.items():
        setattr(address, key, value)
    db.commit()
    db.refresh(address)
    return address


def delete_address(db: Session, user: User, address_id: uuid.UUID) -> None:
    address = get_address(db, user, address_id)
    was_default = address.is_default
    db.delete(address)
    db.flush()
    if was_default:
        remaining = user_repository.list_addresses(db, user.id)
        if remaining:
            remaining[0].is_default = True
    db.commit()
