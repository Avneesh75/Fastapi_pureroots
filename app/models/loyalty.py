"""PureRoots Coins ledger (append-only; only ``created_at``). Cached balances live on ``users``."""
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin

LOYALTY_KINDS = ("earn_pending", "earn_confirmed", "earn_reversed", "redeem", "redeem_reversed", "adjustment")


class LoyaltyTransaction(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One balance movement. ``coins`` is signed: earn_pending +n (to pending), earn_confirmed +n
    (pending → available), earn_reversed −n, redeem −n, redeem_reversed +n, adjustment ±n."""

    __tablename__ = "loyalty_transactions"
    __table_args__ = (
        Index("ix_loyalty_transactions_user_created", "user_id", "created_at"),
        Index("ix_loyalty_transactions_order_id", "order_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))
    coins: Mapped[int] = mapped_column(Integer)
    available_after: Mapped[int] = mapped_column(Integer)
    pending_after: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by: Mapped[str] = mapped_column(String(150), default="system", server_default="system")
