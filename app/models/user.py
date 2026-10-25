"""Customers and their saved addresses."""
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(15))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Profile (0004_user_profile). ``avatar`` is a path relative to MEDIA_ROOT, e.g. ``avatars/<id>/<hex>.webp``.
    avatar: Mapped[str | None] = mapped_column(String(255))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(20))
    # PureRoots Coins cached balances (0005_loyalty); the ledger is ``loyalty_transactions``.
    # ``coins_available`` may go below zero when coins that were already spent are reversed.
    coins_available: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    coins_pending: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    addresses: Mapped[list["Address"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="Address.created_at"
    )

    @property
    def first_name(self) -> str:
        return (self.full_name or "").split(" ")[0]

    @property
    def initials(self) -> str:
        """Up to two initials for the avatar fallback, e.g. ``"Aarav Sharma"`` → ``"AS"``."""
        parts = (self.full_name or "").split()[:2]
        return "".join(part[0] for part in parts).upper() or (self.email or "?")[0].upper()


class Address(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "addresses"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(50), default="Home", server_default="Home")
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(15))
    line1: Mapped[str] = mapped_column(String(255))
    line2: Mapped[str] = mapped_column(String(255), default="", server_default="")
    landmark: Mapped[str] = mapped_column(String(120), default="", server_default="")
    city: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(60))
    pincode: Mapped[str] = mapped_column(String(6))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    user: Mapped[User] = relationship(back_populates="addresses")

    def snapshot(self) -> dict[str, str]:
        """JSON snapshot stored on orders."""
        return {
            "full_name": self.full_name,
            "phone": self.phone,
            "line1": self.line1,
            "line2": self.line2 or "",
            "landmark": self.landmark or "",
            "city": self.city,
            "state": self.state,
            "pincode": self.pincode,
        }
