"""Initial FastAPI-owned schema (ARCHITECTURE §4.2).

Creates: users, addresses, carts, cart_items, wishlist_items, orders, order_items,
order_status_history, payments, coupon_redemptions, reviews, contact_messages,
newsletter_subscribers.

Catalog UUIDs (product_id, variant_id, coupon_id) are plain indexed UUID columns —
there are no foreign keys to Django-owned tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _money() -> sa.Numeric:
    return sa.Numeric(10, 2)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), nullable=False)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _updated_at() -> sa.Column:
    return sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(15), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    # ------------------------------------------------------------------ addresses
    op.create_table(
        "addresses",
        _id(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(50), server_default="Home", nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(15), nullable=False),
        sa.Column("line1", sa.String(255), nullable=False),
        sa.Column("line2", sa.String(255), server_default="", nullable=False),
        sa.Column("landmark", sa.String(120), server_default="", nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state", sa.String(60), nullable=False),
        sa.Column("pincode", sa.String(6), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_addresses_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_addresses")),
    )
    op.create_index(op.f("ix_addresses_user_id"), "addresses", ["user_id"])

    # ------------------------------------------------------------------ carts
    op.create_table(
        "carts",
        _id(),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("session_key", sa.String(64), nullable=True),
        sa.Column("coupon_code", sa.String(40), nullable=True),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_carts_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carts")),
        sa.UniqueConstraint("user_id", name=op.f("uq_carts_user_id")),
        sa.UniqueConstraint("session_key", name=op.f("uq_carts_session_key")),
    )

    op.create_table(
        "cart_items",
        _id(),
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["cart_id"], ["carts.id"], name=op.f("fk_cart_items_cart_id_carts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cart_items")),
        sa.UniqueConstraint("cart_id", "variant_id", name=op.f("uq_cart_items_cart_variant")),
    )
    op.create_index(op.f("ix_cart_items_cart_id"), "cart_items", ["cart_id"])
    op.create_index(op.f("ix_cart_items_variant_id"), "cart_items", ["variant_id"])

    # ------------------------------------------------------------------ wishlist
    op.create_table(
        "wishlist_items",
        _id(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_wishlist_items_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wishlist_items")),
        sa.UniqueConstraint("user_id", "product_id", name=op.f("uq_wishlist_items_user_product")),
    )
    op.create_index(op.f("ix_wishlist_items_user_id"), "wishlist_items", ["user_id"])
    op.create_index(op.f("ix_wishlist_items_product_id"), "wishlist_items", ["product_id"])

    # ------------------------------------------------------------------ orders
    op.create_table(
        "orders",
        _id(),
        sa.Column("order_number", sa.String(32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("payment_status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("delivery_method", sa.String(20), server_default="standard", nullable=False),
        sa.Column("customer_name", sa.String(120), nullable=False),
        sa.Column("customer_email", sa.String(254), nullable=False),
        sa.Column("customer_phone", sa.String(15), nullable=False),
        sa.Column("shipping_address", _json(), nullable=False),
        sa.Column("subtotal", _money(), nullable=False),
        sa.Column("discount_total", _money(), server_default="0", nullable=False),
        sa.Column("shipping_fee", _money(), server_default="0", nullable=False),
        sa.Column("tax_total", _money(), server_default="0", nullable=False),
        sa.Column("grand_total", _money(), nullable=False),
        sa.Column("coupon_code", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_order_number"), "orders", ["order_number"], unique=True)
    op.create_index(op.f("ix_orders_user_id"), "orders", ["user_id"])
    op.create_index(op.f("ix_orders_status"), "orders", ["status"])
    op.create_index(op.f("ix_orders_customer_email"), "orders", ["customer_email"])
    op.create_index(op.f("ix_orders_created_at"), "orders", ["created_at"])

    op.create_table(
        "order_items",
        _id(),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("product_slug", sa.String(255), nullable=False),
        sa.Column("variant_label", sa.String(50), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("unit_mrp", _money(), nullable=False),
        sa.Column("unit_price", _money(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2), server_default="0", nullable=False),
        sa.Column("line_total", _money(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"])
    op.create_index(op.f("ix_order_items_product_id"), "order_items", ["product_id"])
    op.create_index(op.f("ix_order_items_variant_id"), "order_items", ["variant_id"])

    op.create_table(
        "order_status_history",
        _id(),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("changed_by", sa.String(150), server_default="system", nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_status_history_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_status_history")),
    )
    op.create_index(op.f("ix_order_status_history_order_id"), "order_status_history", ["order_id"])

    op.create_table(
        "payments",
        _id(),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_reference", sa.String(120), server_default="", nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("raw_response", _json(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"])

    op.create_table(
        "coupon_redemptions",
        _id(),
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("coupon_code", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("discount_amount", _money(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_coupon_redemptions_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_redemptions")),
    )
    op.create_index(op.f("ix_coupon_redemptions_coupon_id"), "coupon_redemptions", ["coupon_id"])
    op.create_index(op.f("ix_coupon_redemptions_user_id"), "coupon_redemptions", ["user_id"])
    op.create_index(op.f("ix_coupon_redemptions_order_id"), "coupon_redemptions", ["order_id"])

    # ------------------------------------------------------------------ reviews
    op.create_table(
        "reviews",
        _id(),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), server_default="", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("is_approved", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_verified_purchase", sa.Boolean(), server_default=sa.false(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name=op.f("ck_reviews_rating_range")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_reviews_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint("product_id", "user_id", name=op.f("uq_reviews_product_user")),
    )
    op.create_index(op.f("ix_reviews_product_id"), "reviews", ["product_id"])
    op.create_index(op.f("ix_reviews_user_id"), "reviews", ["user_id"])

    # ------------------------------------------------------------------ contact & newsletter
    op.create_table(
        "contact_messages",
        _id(),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("mobile", sa.String(15), nullable=False),
        sa.Column("subject", sa.String(150), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("product_interest", sa.String(200), server_default="", nullable=False),
        sa.Column("city", sa.String(100), server_default="", nullable=False),
        sa.Column("status", sa.String(20), server_default="new", nullable=False),
        sa.Column("sheets_sync_status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("sheets_error", sa.Text(), nullable=True),
        sa.Column("sheets_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), server_default="", nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_messages")),
    )
    op.create_index(op.f("ix_contact_messages_status"), "contact_messages", ["status"])
    op.create_index(op.f("ix_contact_messages_sheets_sync_status"), "contact_messages", ["sheets_sync_status"])

    op.create_table(
        "newsletter_subscribers",
        _id(),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("source", sa.String(50), server_default="", nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsletter_subscribers")),
    )
    op.create_index(op.f("ix_newsletter_subscribers_email"), "newsletter_subscribers", ["email"], unique=True)


def downgrade() -> None:
    op.drop_table("newsletter_subscribers")
    op.drop_table("contact_messages")
    op.drop_table("reviews")
    op.drop_table("coupon_redemptions")
    op.drop_table("payments")
    op.drop_table("order_status_history")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("wishlist_items")
    op.drop_table("cart_items")
    op.drop_table("carts")
    op.drop_table("addresses")
    op.drop_table("users")
