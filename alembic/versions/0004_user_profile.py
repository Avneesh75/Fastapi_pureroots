"""Customer profile: photo, date of birth and gender.

* ``users.avatar`` varchar(255), nullable — path relative to MEDIA_ROOT (``avatars/<user_id>/<hex>.webp``)
* ``users.date_of_birth`` date, nullable
* ``users.gender`` varchar(20), nullable — ``female`` | ``male`` | ``other`` | ``prefer_not``

Adding nullable columns without defaults is a metadata-only change in PostgreSQL.

Revision ID: 0004_user_profile
Revises: 0003_upi
Create Date: 2026-09-30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_user_profile"
down_revision: str | None = "0003_upi"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("avatar", sa.String(255), nullable=True))
        batch.add_column(sa.Column("date_of_birth", sa.Date(), nullable=True))
        batch.add_column(sa.Column("gender", sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("gender")
        batch.drop_column("date_of_birth")
        batch.drop_column("avatar")
