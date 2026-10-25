"""Direct UPI payments.

* ``orders.payment_status`` varchar(20) → varchar(30): the new value ``awaiting_verification`` is 21 characters.
* ``uq_payments_upi_utr``: partial unique index on ``payments.provider_reference`` for live UPI payments
  (``provider = 'upi'``, a UTR is set and the payment was not rejected), so one UPI transaction
  reference (UTR) can never confirm two orders. The service checks this first and returns a friendly 409;
  the index is the race-proof backstop.

Widening a varchar is a metadata-only change in PostgreSQL; existing rows stay valid.

Revision ID: 0003_upi
Revises: 0002_offers
Create Date: 2026-09-30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_upi"
down_revision: str | None = "0002_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_payments_upi_utr"
WHERE = sa.text("provider = 'upi' AND provider_reference <> '' AND status <> 'rejected'")


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.alter_column(
            "payment_status",
            existing_type=sa.String(20),
            type_=sa.String(30),
            existing_nullable=False,
            existing_server_default="pending",
        )
    op.create_index(
        INDEX,
        "payments",
        ["provider_reference"],
        unique=True,
        postgresql_where=WHERE,
        sqlite_where=WHERE,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="payments")
    op.execute("UPDATE orders SET payment_status = 'pending' WHERE payment_status = 'awaiting_verification'")
    with op.batch_alter_table("orders") as batch:
        batch.alter_column(
            "payment_status",
            existing_type=sa.String(30),
            type_=sa.String(20),
            existing_nullable=False,
            existing_server_default="pending",
        )
