"""Wishlist request model (responses are lists of ``ProductCard``)."""
import uuid

from app.schemas.common import AppSchema


class WishlistAdd(AppSchema):
    product_id: uuid.UUID
