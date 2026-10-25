"""PureRoots Coins request/response models."""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import AppSchema, Trimmed
from app.schemas.order import CHANGED_BY_PATTERN

MAX_ADJUSTMENT = 100_000


class CartCoinsToggle(AppSchema):
    use: bool


class LoyaltyRuleOut(BaseModel):
    enabled: bool
    earn_percent: int = Field(description="Percent of what was paid for the goods credited as coins on delivery.")
    step_amount: float = Field(description="Each full step of this order amount unlocks 1/max_steps of the balance.")
    max_steps: int
    text: str


class LoyaltyTransactionOut(BaseModel):
    id: uuid.UUID
    kind: str = Field(description="earn_pending | earn_confirmed | earn_reversed | redeem | redeem_reversed | adjustment")
    kind_label: str
    coins: int = Field(description="Signed: + credit, − debit.")
    available_after: int
    pending_after: int
    note: str
    order_number: str | None = None
    created_by: str = "system"
    created_at: datetime


class LoyaltySummaryOut(BaseModel):
    available: int = Field(description="Coins usable now (may be negative after reversing spent coins).")
    pending: int = Field(description="Coins promised on orders not delivered yet.")
    lifetime_earned: int
    rule: LoyaltyRuleOut
    transactions: list[LoyaltyTransactionOut]


class InternalCoinsAdjust(AppSchema):
    coins: int = Field(description=f"Signed, non-zero, at most {MAX_ADJUSTMENT:,} either way.")
    note: Annotated[str, Trimmed, Field(min_length=3, max_length=300)]
    changed_by: Annotated[str, Trimmed, Field(pattern=CHANGED_BY_PATTERN, examples=["admin:priya"])] = "system"

    @field_validator("coins")
    @classmethod
    def _check_coins(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Enter a non-zero number of coins (negative to deduct).")
        if abs(value) > MAX_ADJUSTMENT:
            raise ValueError(f"Adjust at most {MAX_ADJUSTMENT:,} coins at a time.")
        return value


class CoinsAdjustmentOut(BaseModel):
    user_id: uuid.UUID
    available: int
    pending: int
    transaction: LoyaltyTransactionOut
