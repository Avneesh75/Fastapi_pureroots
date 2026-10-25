"""Checkout, order and internal status-change models."""
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from app.schemas.address import AddressFields
from app.schemas.common import AppSchema, Email, IndianPhone, PersonName, Trimmed
from app.services.payments.upi import PAYER_VPA_MESSAGE, UTR_MESSAGE, is_valid_utr, is_valid_vpa, normalize_utr

CHANGED_BY_PATTERN = r"^(system|customer|admin:[^\s].{0,140})$"


def _none_to_empty(value: object) -> object:
    """Django's client sends ``"note": null`` for a blank note."""
    return "" if value is None else value


OptionalNote = BeforeValidator(_none_to_empty)
PAYMENT_STATUS_DESCRIPTION = "pending | cod_pending | awaiting_verification | paid | failed | refunded"


class CheckoutCustomer(AppSchema):
    full_name: PersonName
    email: Email
    phone: IndianPhone


class ShippingAddressIn(AddressFields):
    """Postal address snapshot used at checkout."""


class CheckoutRequest(AppSchema):
    customer: CheckoutCustomer
    shipping_address: ShippingAddressIn | None = None
    address_id: uuid.UUID | None = None
    save_address: bool = False
    delivery_method: Literal["standard", "express"] = "standard"
    payment_method: Annotated[str, Trimmed, Field(min_length=2, max_length=20)] = "cod"
    notes: Annotated[str, Trimmed, Field(max_length=500)] = ""

    @model_validator(mode="after")
    def _require_address(self) -> "CheckoutRequest":
        if self.shipping_address is None and self.address_id is None:
            raise ValueError("Please provide a shipping address.")
        return self


class OrderLookupRequest(AppSchema):
    order_number: Annotated[str, Trimmed, Field(min_length=5, max_length=32)]
    email: Email


class PaymentAccessMixin(AppSchema):
    """Guests prove access with the order's email; signed-in owners don't need it."""

    email: Email | None = None


class OrderPayRequest(PaymentAccessMixin):
    pass


class RazorpayVerifyRequest(PaymentAccessMixin):
    order_number: Annotated[str, Trimmed, Field(min_length=5, max_length=32)]
    razorpay_order_id: Annotated[str, Trimmed, Field(min_length=1, max_length=64)]
    razorpay_payment_id: Annotated[str, Trimmed, Field(min_length=1, max_length=64)]
    razorpay_signature: Annotated[str, Trimmed, Field(min_length=1, max_length=256)]


class UpiSubmitRequest(PaymentAccessMixin):
    """The customer's UPI transaction reference (UTR) after paying the store's UPI ID."""

    utr: Annotated[str, Field(max_length=40, examples=["123456789012"])]
    payer_vpa: Annotated[str, Trimmed, Field(max_length=320, examples=["asha@okbank"])] | None = None

    @field_validator("utr", mode="before")
    @classmethod
    def _check_utr(cls, value: object) -> str:
        if not isinstance(value, str) or not is_valid_utr(value):
            raise ValueError(UTR_MESSAGE)
        return normalize_utr(value)

    @field_validator("payer_vpa")
    @classmethod
    def _check_payer_vpa(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not is_valid_vpa(value):
            raise ValueError(PAYER_VPA_MESSAGE)
        return value.lower()


class InternalStatusChange(AppSchema):
    status: Annotated[str, Trimmed, Field(min_length=3, max_length=20)]
    note: Annotated[str, OptionalNote, Trimmed, Field(max_length=1000)] = ""
    changed_by: Annotated[
        str,
        Trimmed,
        Field(pattern=CHANGED_BY_PATTERN, examples=["admin:priya"]),
    ] = "system"


class InternalUpiVerify(AppSchema):
    """Staff decision on a submitted UPI payment (after checking the UTR in the bank/UPI app)."""

    decision: Literal["approve", "reject"]
    note: Annotated[str, OptionalNote, Trimmed, Field(max_length=500)] = ""
    changed_by: Annotated[str, Trimmed, Field(pattern=CHANGED_BY_PATTERN, examples=["admin:priya"])] = "system"


class OrderItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    variant_id: uuid.UUID
    product_name: str
    product_slug: str
    variant_label: str
    sku: str
    image_url: str
    unit_mrp: float
    unit_original_price: float = Field(description="Selling price per unit at order time, before offers.")
    unit_price: float = Field(description="Final (post-offer) unit price.")
    quantity: int
    tax_rate: float
    line_total: float = Field(description="Post-offer line total.")
    offer_discount: float = Field(description="Offer discount for the whole line (0 when none).")
    offer_id: uuid.UUID | None = None
    offer_name: str | None = None
    offer_label: str | None = Field(None, examples=["20% OFF"])


class StatusHistoryOut(BaseModel):
    from_status: str | None
    to_status: str
    note: str
    changed_by: str
    created_at: datetime


class TimelineStep(BaseModel):
    status: str
    label: str
    reached: bool
    at: datetime | None


class OrderSummary(BaseModel):
    order_number: str
    status: str
    status_label: str
    payment_status: str = Field(description=PAYMENT_STATUS_DESCRIPTION)
    payment_status_label: str = ""
    grand_total: float
    offer_discount_total: float
    item_count: int
    placed_at: datetime
    first_item_image: str | None
    coins_redeemed: int = 0
    coins_earned: int = 0
    coins_status: Literal["pending", "credited", "reversed"] | None = None


class PaymentPrefill(BaseModel):
    name: str
    email: str
    contact: str


class PaymentPayload(BaseModel):
    """Everything Razorpay Checkout needs. ``key_id`` is the PUBLIC key id; the secret never leaves the server."""

    provider: Literal["razorpay"] = "razorpay"
    key_id: str
    razorpay_order_id: str
    amount: int = Field(description="Amount in paise, from the server-computed grand_total.")
    currency: str = "INR"
    name: str
    description: str
    prefill: PaymentPrefill
    order_number: str


class UpiPaymentPayload(BaseModel):
    """Direct UPI: pay ``amount`` to ``vpa`` (QR of ``upi_uri`` or the "Open UPI app" button), then submit the UTR."""

    provider: Literal["upi"] = "upi"
    vpa: str
    payee_name: str
    amount: float = Field(description="Rupees, from the server-computed grand_total.")
    currency: str = "INR"
    upi_uri: str = Field(examples=["upi://pay?pa=pureroots@okicici&pn=PureRoots%20Aahar&am=878.00&cu=INR&tn=PureRoots%20order%20PR-260930-ABCDE&tr=PR-260930-ABCDE"])
    note: str = Field(description="Transaction note (`tn`), e.g. 'PureRoots order PR-…'.")
    order_number: str
    expires_at: datetime = Field(description="Unpaid orders with no UTR submitted are cancelled after this.")


class UpiPaymentState(UpiPaymentPayload):
    """Where a direct-UPI order's payment stands (on every OrderDetail of a UPI order)."""

    state: Literal["awaiting_payment", "awaiting_verification", "rejected", "paid", "refunded", "expired", "cancelled"]
    can_submit: bool = Field(description="True while POST /orders/{n}/upi-submit is allowed.")
    utr: str | None = None
    payer_vpa: str | None = None
    submitted_at: datetime | None = None
    rejection_reason: str | None = None


class OrderDetail(BaseModel):
    id: uuid.UUID
    order_number: str
    status: str
    status_label: str
    payment_status: str = Field(description=PAYMENT_STATUS_DESCRIPTION)
    payment_status_label: str = ""
    payment_method: str
    delivery_method: str
    customer_name: str
    customer_email: str
    customer_phone: str
    shipping_address: dict[str, Any]
    items_total: float = Field(description="subtotal + offer_discount_total (before offers).")
    offer_discount_total: float
    subtotal: float = Field(description="Post-offer subtotal.")
    discount_total: float = Field(description="Coupon discount.")
    shipping_fee: float
    tax_total: float
    grand_total: float = Field(description="subtotal - discount_total - coins_redeemed + shipping_fee.")
    coupon_code: str | None
    coins_redeemed: int = Field(0, description="PureRoots Coins used on this order (₹1 each).")
    coins_earned: int = Field(0, description="Coins this order earns: pending until delivered.")
    coins_status: Literal["pending", "credited", "reversed"] | None = Field(
        None, description="pending (until delivery) | credited | reversed (cancelled/refunded); null when none."
    )
    notes: str
    item_count: int
    placed_at: datetime
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut]
    status_history: list[StatusHistoryOut]
    can_cancel: bool
    can_pay: bool = Field(False, description="True for a pending, unpaid Razorpay order (POST /orders/{n}/pay).")
    timeline: list[TimelineStep]
    payment: Annotated[PaymentPayload | UpiPaymentPayload, Field(discriminator="provider")] | None = Field(
        None,
        description=(
            "Set on checkout and pay-retry responses of Razorpay orders (open Razorpay Checkout with it) and on "
            "the checkout response of direct-UPI orders (`provider: \"upi\"`)."
        ),
    )
    upi: UpiPaymentState | None = Field(None, description="Direct-UPI orders only: payment instructions and state.")


# ---- Checkout options ----


class DeliveryMethodOut(BaseModel):
    code: str
    label: str
    fee: float
    eta: str
    description: str


class PaymentMethodOut(BaseModel):
    code: str
    label: str
    description: str
    enabled: bool


class CheckoutOptionsOut(BaseModel):
    delivery_methods: list[DeliveryMethodOut]
    payment_methods: list[PaymentMethodOut]
    free_shipping_threshold: float
    states: list[str]
