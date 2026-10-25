"""Reusable column mixins and portable types (ARCHITECTURE §3)."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Numeric, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from app.core.clock import utcnow


def json_type() -> TypeEngine:
    """JSON on SQLite, JSONB on PostgreSQL."""
    return JSON().with_variant(JSONB(), "postgresql")


def money_type() -> Numeric:
    """``numeric(10,2)`` returning ``Decimal``."""
    return Numeric(10, 2)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )
