"""READ-ONLY mappings of Django-owned tables (ARCHITECTURE §1.1, §4.1).

!!! DO NOT WRITE THROUGH THESE MODELS IN APPLICATION CODE !!!

* They live on ``CatalogBase``, whose metadata Alembic never sees, so FastAPI
  never creates, alters or drops these tables. Django owns their migrations.
* Relationships are ``viewonly``. Python-side defaults exist only so the test
  suite can insert fixture rows into SQLite via ``CatalogBase.metadata.create_all``.
* Stock is changed exclusively through Django's internal inventory API
  (see ``app/services/inventory_client.py``).
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Table, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.database import CatalogBase
from app.models.base import json_type


class _CatalogTimestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Category(_CatalogTimestamps, CatalogBase):
    """``categories`` (Django ``catalog.Category``). Read-only."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(220), unique=True)
    short_description: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    accent_color: Mapped[str] = mapped_column(String(7), default="#6F8F6A")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    seo_title: Mapped[str] = mapped_column(String(160), default="")
    seo_description: Mapped[str] = mapped_column(String(300), default="")


class Product(_CatalogTimestamps, CatalogBase):
    """``products`` (Django ``catalog.Product``). Read-only."""

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220), unique=True)
    short_description: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    key_benefits: Mapped[str] = mapped_column(Text, default="")
    ingredients: Mapped[str] = mapped_column(Text, default="")
    nutrition: Mapped[list[Any] | None] = mapped_column(json_type(), default=list)
    storage_info: Mapped[str] = mapped_column(Text, default="")
    shelf_life: Mapped[str] = mapped_column(String(120), default="")
    how_to_use: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(200), default="")
    certifications: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[Any] | None] = mapped_column(json_type(), default=list)
    search_keywords: Mapped[str] = mapped_column(Text, default="")
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("5.00"))
    status: Mapped[str] = mapped_column(String(20), default="active")
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bestseller: Mapped[bool] = mapped_column(Boolean, default=False)
    popularity: Mapped[int] = mapped_column(Integer, default=0)
    seo_title: Mapped[str] = mapped_column(String(160), default="")
    seo_description: Mapped[str] = mapped_column(String(300), default="")

    category: Mapped[Category] = relationship(viewonly=True)
    variants: Mapped[list["ProductVariant"]] = relationship(
        viewonly=True, order_by="ProductVariant.sort_order", back_populates="product"
    )
    images: Mapped[list["ProductImage"]] = relationship(viewonly=True, order_by="ProductImage.sort_order")


class ProductVariant(_CatalogTimestamps, CatalogBase):
    """``product_variants`` (Django ``catalog.ProductVariant``). Read-only."""

    __tablename__ = "product_variants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("products.id"))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(50))
    weight_grams: Mapped[int] = mapped_column(Integer, default=0)
    mrp: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    cost_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=10)

    product: Mapped[Product] = relationship(viewonly=True, back_populates="variants")


class ProductImage(_CatalogTimestamps, CatalogBase):
    """``product_images`` (Django ``catalog.ProductImage``). Read-only."""

    __tablename__ = "product_images"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("products.id"))
    image: Mapped[str] = mapped_column(String(255))
    alt_text: Mapped[str] = mapped_column(String(200), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Coupon(_CatalogTimestamps, CatalogBase):
    """``coupons`` (Django ``catalog.Coupon``). Read-only; redemptions are recorded in FastAPI's table."""

    __tablename__ = "coupons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    discount_type: Mapped[str] = mapped_column(String(10))
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    min_order_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    per_user_limit: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # OFFERS_AND_ADMIN §2: when False, the coupon is rejected on carts that have an offer discount.
    combinable_with_offers: Mapped[bool] = mapped_column(Boolean, default=False)


class StockLevel(_CatalogTimestamps, CatalogBase):
    """``stock_levels`` (Django ``inventory.StockLevel``). Read-only. ``available = on_hand - reserved``."""

    __tablename__ = "stock_levels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("product_variants.id"))
    # warehouses is not mapped here; the FK exists only on the Django side.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- offers (Django ``offers`` app)

offers_categories = Table(
    "offers_categories",
    CatalogBase.metadata,
    Column("offer_id", Uuid, ForeignKey("offers.id"), primary_key=True),
    Column("category_id", Uuid, ForeignKey("categories.id"), primary_key=True),
)
"""``offers_categories`` M2M (Django-owned, read-only). Django's own ``id`` column is not mapped."""

offers_products = Table(
    "offers_products",
    CatalogBase.metadata,
    Column("offer_id", Uuid, ForeignKey("offers.id"), primary_key=True),
    Column("product_id", Uuid, ForeignKey("products.id"), primary_key=True),
)
"""``offers_products`` M2M (Django-owned, read-only). Django's own ``id`` column is not mapped."""


class Offer(_CatalogTimestamps, CatalogBase):
    """``offers`` (Django ``offers.Offer``, OFFERS_AND_ADMIN §2). Read-only.

    Load them as ``pureroots_pricing.OfferRule`` through ``offer_repository.live_rules``.
    """

    __tablename__ = "offers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(255), default="")
    discount_type: Mapped[str] = mapped_column(String(10))  # percent | flat
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    scope: Mapped[str] = mapped_column(String(20), default="all")  # all | category | products
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    min_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- CMS (Django ``cms`` app)


class SiteSettings(CatalogBase):
    """``cms_site_settings`` singleton (Django ``cms.SiteSettings``). Read-only.

    Only the columns FastAPI needs are mapped: the maintenance switch read by
    ``app/core/maintenance.py`` (super admins flip it in the Django admin API).
    """

    __tablename__ = "cms_site_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_name: Mapped[str] = mapped_column(String(80), default="PureRoots")
    maintenance_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    maintenance_title: Mapped[str] = mapped_column(String(120), default="")
    maintenance_message: Mapped[str] = mapped_column(Text, default="")
    maintenance_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
