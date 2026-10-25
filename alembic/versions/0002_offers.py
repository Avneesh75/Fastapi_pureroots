"""Offer snapshots on orders (OFFERS_AND_ADMIN §3).

* ``orders.offer_discount_total``  numeric(10,2) NOT NULL DEFAULT 0
* ``order_items.unit_original_price`` numeric(10,2) NOT NULL DEFAULT 0, back-filled from ``unit_price``
  (orders placed before offers existed paid the selling price, so original == final)
* ``order_items.offer_discount``   numeric(10,2) NOT NULL DEFAULT 0 (line total)
* ``order_items.offer_id``         uuid NULL (no FK: ``offers`` is Django-owned)
* ``order_items.offer_name``       varchar(120) NULL
* ``order_items.offer_label``      varchar(40) NULL ("20% OFF" / "₹100 OFF", from ``pureroots_pricing.offer_label``)

Every new column has a server default or is nullable, so existing rows stay valid.

Revision ID: 0002_offers
Revises: 0001_initial
Create Date: 2026-09-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_offers"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _money() -> sa.Numeric:
    return sa.Numeric(10, 2)


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("offer_discount_total", _money(), server_default="0", nullable=False),
    )
    op.add_column(
        "order_items",
        sa.Column("unit_original_price", _money(), server_default="0", nullable=False),
    )
    op.add_column(
        "order_items",
        sa.Column("offer_discount", _money(), server_default="0", nullable=False),
    )
    op.add_column("order_items", sa.Column("offer_id", sa.Uuid(), nullable=True))
    op.add_column("order_items", sa.Column("offer_name", sa.String(120), nullable=True))
    op.add_column("order_items", sa.Column("offer_label", sa.String(40), nullable=True))
    op.execute("UPDATE order_items SET unit_original_price = unit_price")


def downgrade() -> None:
    op.drop_column("order_items", "offer_label")
    op.drop_column("order_items", "offer_name")
    op.drop_column("order_items", "offer_id")
    op.drop_column("order_items", "offer_discount")
    op.drop_column("order_items", "unit_original_price")
    op.drop_column("orders", "offer_discount_total")
