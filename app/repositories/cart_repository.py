"""Cart and wishlist queries."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.cart import Cart, CartItem, WishlistItem


def get_by_user(db: Session, user_id: uuid.UUID) -> Cart | None:
    return db.scalar(select(Cart).where(Cart.user_id == user_id).options(selectinload(Cart.items)))


def get_by_session(db: Session, session_key: str) -> Cart | None:
    return db.scalar(
        select(Cart).where(Cart.session_key == session_key, Cart.user_id.is_(None)).options(selectinload(Cart.items))
    )


def create(db: Session, *, user_id: uuid.UUID | None = None, session_key: str | None = None) -> Cart:
    cart = Cart(user_id=user_id, session_key=None if user_id else session_key)
    db.add(cart)
    db.flush()
    return cart


def find_item(cart: Cart, variant_id: uuid.UUID) -> CartItem | None:
    return next((item for item in cart.items if item.variant_id == variant_id), None)


# ---- wishlist ----


def list_wishlist(db: Session, user_id: uuid.UUID) -> list[WishlistItem]:
    stmt = select(WishlistItem).where(WishlistItem.user_id == user_id).order_by(WishlistItem.created_at.desc())
    return list(db.scalars(stmt))


def get_wishlist_item(db: Session, user_id: uuid.UUID, product_id: uuid.UUID) -> WishlistItem | None:
    return db.scalar(
        select(WishlistItem).where(WishlistItem.user_id == user_id, WishlistItem.product_id == product_id)
    )
