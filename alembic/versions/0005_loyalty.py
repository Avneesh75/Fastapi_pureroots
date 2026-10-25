"""PureRoots Coins (loyalty cashback).

* ``loyalty_transactions``: append-only ledger (id, user_id→users CASCADE, order_id→orders SET NULL, kind,
  signed coins, available_after, pending_after, note, created_by, created_at); indexes on
  (user_id, created_at) and (order_id).
* ``users.coins_available`` / ``users.coins_pending``: cached balances (int, default 0), updated in the
  same transaction as the ledger row under a row lock.
* ``orders.coins_redeemed`` / ``orders.coins_earned``: coins used on the order and coins promised on delivery.
* ``carts.use_coins``: the customer's "use my coins" toggle.

Adding NOT NULL columns with a constant default is a metadata-only change in PostgreSQL 11+.

Revision ID: 0005_loyalty
Revises: 0004_user_profile
Create Date: 2026-10-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_loyalty"
down_revision: str | None = "0004_user_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("coins_available", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("coins_pending", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("coins_redeemed", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("coins_earned", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("carts") as batch:
        batch.add_column(sa.Column("use_coins", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "loyalty_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("coins", sa.Integer(), nullable=False),
        sa.Column("available_after", sa.Integer(), nullable=False),
        sa.Column("pending_after", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(150), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_loyalty_transactions_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_loyalty_transactions_order_id_orders", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_loyalty_transactions"),
    )
    op.create_index("ix_loyalty_transactions_user_created", "loyalty_transactions", ["user_id", "created_at"])
    op.create_index("ix_loyalty_transactions_order_id", "loyalty_transactions", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_loyalty_transactions_order_id", table_name="loyalty_transactions")
    op.drop_index("ix_loyalty_transactions_user_created", table_name="loyalty_transactions")
    op.drop_table("loyalty_transactions")
    with op.batch_alter_table("carts") as batch:
        batch.drop_column("use_coins")
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("coins_earned")
        batch.drop_column("coins_redeemed")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("coins_pending")
        batch.drop_column("coins_available")
