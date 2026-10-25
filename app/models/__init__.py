"""SQLAlchemy models.

Owned tables (``Base``, migrated by Alembic) and read-only Django tables
(``CatalogBase`` in ``catalog.py``). Importing this package registers all models.
"""
from app.models.cart import Cart, CartItem, WishlistItem
from app.models.catalog import (
    Category,
    Coupon,
    Offer,
    Product,
    ProductImage,
    ProductVariant,
    SiteSettings,
    StockLevel,
)
from app.models.contact import ContactMessage, NewsletterSubscriber
from app.models.loyalty import LoyaltyTransaction
from app.models.order import CouponRedemption, Order, OrderItem, OrderStatusHistory, Payment
from app.models.review import Review
from app.models.user import Address, User

__all__ = [
    "Address",
    "Cart",
    "CartItem",
    "Category",
    "ContactMessage",
    "Coupon",
    "CouponRedemption",
    "LoyaltyTransaction",
    "NewsletterSubscriber",
    "Offer",
    "Order",
    "OrderItem",
    "OrderStatusHistory",
    "Payment",
    "Product",
    "ProductImage",
    "ProductVariant",
    "Review",
    "SiteSettings",
    "StockLevel",
    "User",
    "WishlistItem",
]
