"""Cart request/response models (ARCHITECTURE §5 "Cart")."""
import uuid
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.schemas.catalog import OfferOut
from app.schemas.common import AppSchema, Trimmed


class CartItemCreate(AppSchema):
    variant_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=100)] = 1


class CartItemUpdate(AppSchema):
    quantity: Annotated[int, Field(ge=0, le=100)] | None = None
    variant_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _require_change(self) -> "CartItemUpdate":
        if self.quantity is None and self.variant_id is None:
            raise ValueError("Provide a quantity or a variant to update.")
        return self


class CouponApply(AppSchema):
    code: Annotated[str, Trimmed, Field(min_length=2, max_length=40)]


class CartVariantOption(BaseModel):
    id: uuid.UUID
    label: str
    price: float
    mrp: float
    in_stock: bool
    final_price: float = Field(description="Selling price minus the best live offer (catalog display rule).")


class CartItemOut(BaseModel):
    id: uuid.UUID
    variant_id: uuid.UUID
    product_id: uuid.UUID
    product_slug: str
    product_name: str
    variant_label: str
    sku: str
    image_url: str
    unit_mrp: float
    unit_original_price: float = Field(description="Selling price per unit, before offers.")
    unit_price: float = Field(description="Post-offer unit price (display; line_discount is authoritative).")
    quantity: int
    line_original: float = Field(description="unit_original_price x quantity.")
    line_discount: float = Field(description="Offer discount for the whole line.")
    line_total: float = Field(description="line_original - line_discount.")
    offer: OfferOut | None = None
    available_quantity: int
    in_stock: bool
    variants: list[CartVariantOption]


class CartTotals(BaseModel):
    mrp_total: float
    items_total: float = Field(description="Sum of selling price x qty, before offers.")
    offer_discount: float = Field(description="Sum of line offer discounts.")
    subtotal: float = Field(description="Post-offer subtotal: items_total - offer_discount.")
    savings: float = Field(
        description="MRP savings: mrp_total - items_total (offer and coupon discounts are reported separately)."
    )
    coupon_discount: float
    shipping: float
    tax_included: float
    total: float
    currency: str = "INR"
    free_shipping_threshold: float
    amount_to_free_shipping: float
    # ---- PureRoots Coins (1 coin = ₹1). `total` above is already after coins_discount.
    loyalty_enabled: bool = False
    coins_use: bool = Field(False, description="The customer's 'use my coins' toggle (always false for guests).")
    coins_available: int = 0
    coins_pending: int = 0
    coins_max_redeemable: int = Field(0, description="Most coins usable on this cart right now.")
    coins_redeemed: int = Field(0, description="Coins applied to this cart (0 when the toggle is off).")
    coins_discount: float = 0.0
    coins_to_earn: int = Field(0, description="Coins this order earns on delivery (also computed for guests).")
    coins_rule: str = ""
    coins_redeem_fraction: str = Field("0", description='"0" | "1/3" | "2/3" | "all"')
    coins_amount_to_next_step: float | None = Field(None, description="₹ more needed to unlock the next step.")
    coins_blocked_reason: str | None = Field(None, description="Why coins can't be used now (signed-in only).")


class CartOut(BaseModel):
    id: uuid.UUID | None
    item_count: int
    coupon_code: str | None
    items: list[CartItemOut]
    totals: CartTotals
    messages: list[str]
