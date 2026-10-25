"""Orders, line items, status history, payments and coupon redemptions."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, json_type, money_type

UPI_UTR_INDEX = "uq_payments_upi_utr"
UPI_UTR_INDEX_WHERE = text("provider = 'upi' AND provider_reference <> '' AND status <> 'rejected'")


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_created_at", "created_at"),)

    order_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending", server_default="pending")
    # pending | cod_pending | awaiting_verification | paid | failed | refunded (0003 widened it to 30 chars)
    payment_status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    payment_method: Mapped[str] = mapped_column(String(20))
    delivery_method: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard")
    customer_name: Mapped[str] = mapped_column(String(120))
    customer_email: Mapped[str] = mapped_column(String(254), index=True)
    customer_phone: Mapped[str] = mapped_column(String(15))
    shipping_address: Mapped[dict[str, Any]] = mapped_column(json_type())
    subtotal: Mapped[Decimal] = mapped_column(money_type())
    discount_total: Mapped[Decimal] = mapped_column(money_type(), default=Decimal("0.00"), server_default="0")
    # Σ order_items.offer_discount. ``subtotal`` is post-offer; ``discount_total`` is the coupon discount.
    offer_discount_total: Mapped[Decimal] = mapped_column(
        money_type(), default=Decimal("0.00"), server_default="0"
    )
    shipping_fee: Mapped[Decimal] = mapped_column(money_type(), default=Decimal("0.00"), server_default="0")
    tax_total: Mapped[Decimal] = mapped_column(money_type(), default=Decimal("0.00"), server_default="0")
    grand_total: Mapped[Decimal] = mapped_column(money_type())
    coupon_code: Mapped[str | None] = mapped_column(String(40))
    # PureRoots Coins (0005_loyalty): coins used on this order (₹1 each, already deducted from grand_total)
    # and coins promised on delivery (pending until then).
    coins_redeemed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    coins_earned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.created_at"
    )
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderStatusHistory.created_at"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Payment.created_at"
    )

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)


class OrderItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    product_name: Mapped[str] = mapped_column(String(200))
    product_slug: Mapped[str] = mapped_column(String(255))
    variant_label: Mapped[str] = mapped_column(String(50))
    sku: Mapped[str] = mapped_column(String(64))
    unit_mrp: Mapped[Decimal] = mapped_column(money_type())
    unit_original_price: Mapped[Decimal] = mapped_column(money_type(), server_default="0")  # selling price
    unit_price: Mapped[Decimal] = mapped_column(money_type())  # final (post-offer) unit price
    quantity: Mapped[int] = mapped_column(Integer)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"), server_default="0")
    line_total: Mapped[Decimal] = mapped_column(money_type())  # line_original - offer_discount
    offer_discount: Mapped[Decimal] = mapped_column(money_type(), default=Decimal("0.00"), server_default="0")
    offer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # no FK: offers is Django-owned
    offer_name: Mapped[str | None] = mapped_column(String(120))
    offer_label: Mapped[str | None] = mapped_column(String(40))  # "20% OFF" / "₹100 OFF"

    order: Mapped[Order] = relationship(back_populates="items")


class OrderStatusHistory(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only log of status transitions (only ``created_at``)."""

    __tablename__ = "order_status_history"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    changed_by: Mapped[str] = mapped_column(String(150), default="system", server_default="system")

    order: Mapped[Order] = relationship(back_populates="status_history")


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        # A direct-UPI transaction reference (UTR) can back only one live payment (0003_upi).
        Index(
            UPI_UTR_INDEX,
            "provider_reference",
            unique=True,
            postgresql_where=UPI_UTR_INDEX_WHERE,
            sqlite_where=UPI_UTR_INDEX_WHERE,
        ),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    provider_reference: Mapped[str] = mapped_column(String(120), default="", server_default="")
    amount: Mapped[Decimal] = mapped_column(money_type())
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(json_type())

    order: Mapped[Order] = relationship(back_populates="payments")


class CouponRedemption(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only record of a coupon used on an order (only ``created_at``)."""

    __tablename__ = "coupon_redemptions"

    coupon_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    coupon_code: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    discount_amount: Mapped[Decimal] = mapped_column(money_type())
