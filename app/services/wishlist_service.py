"""Wishlist of products for signed-in customers."""
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import NotFoundError
from app.models.cart import WishlistItem
from app.models.user import User
from app.repositories import cart_repository, catalog_repository
from app.schemas.catalog import ProductCard
from app.services.catalog_service import PRODUCT_NOT_FOUND, build_cards


def list_cards(db: Session, user: User, settings: Settings) -> list[ProductCard]:
    """Wishlisted products (newest first); products no longer listed are skipped."""
    items = cart_repository.list_wishlist(db, user.id)
    rows = {row.product.id: row for row in catalog_repository.get_rows_by_ids(db, [i.product_id for i in items])}
    ordered = [rows[item.product_id] for item in items if item.product_id in rows]
    return build_cards(db, ordered, settings)


def add(db: Session, user: User, product_id: uuid.UUID, settings: Settings) -> list[ProductCard]:
    if catalog_repository.get_product_row(db, str(product_id)) is None:
        raise NotFoundError(PRODUCT_NOT_FOUND)
    if cart_repository.get_wishlist_item(db, user.id, product_id) is None:
        db.add(WishlistItem(user_id=user.id, product_id=product_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # added concurrently — idempotent
    return list_cards(db, user, settings)


def remove(db: Session, user: User, product_id: uuid.UUID, settings: Settings) -> list[ProductCard]:
    item = cart_repository.get_wishlist_item(db, user.id, product_id)
    if item is not None:
        db.delete(item)
        db.commit()
    return list_cards(db, user, settings)
