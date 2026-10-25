"""Pure pricing functions (ARCHITECTURE §5 "Pricing rules", OFFERS_AND_ADMIN §3). No I/O, only ``Decimal``.

Offer maths is NOT done here: callers get each line's ``offer_discount`` from
``pureroots_pricing.price_lines`` and pass it on ``PriceLine``.

* items_total     = Σ price × qty                  (selling prices, before offers)
* offer_discount  = Σ line offer discounts
* subtotal        = items_total − offer_discount   (post-offer)
* savings         = mrp_total − items_total
* coupon_discount = percent of subtotal (capped at max_discount) or flat; needs subtotal ≥ min_order_value
* shipping        = standard fee unless (subtotal − discount) ≥ threshold → 0; express always charged
* coins_discount  = PureRoots Coins redeemed (₹1 each; the amount comes from ``loyalty_rules``), capped at
                    subtotal − coupon_discount; shipping is never paid with coins and the free-shipping
                    check ignores coins
* tax_included    = Σ included GST per line after spreading coupon + coins discounts proportionally
* total           = subtotal − coupon_discount − coins_discount + shipping
* all rounding    = ROUND_HALF_UP to 2 decimal places
"""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def to_decimal(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def q2(value: Decimal | int | float | str | None) -> Decimal:
    """Round to 2 dp with ROUND_HALF_UP."""
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money(value: Decimal | int | float | str | None) -> float:
    """JSON representation of a money amount (a number, e.g. ``199.0``)."""
    return float(q2(value))


def format_inr(value: Decimal | int | float | str) -> str:
    amount = q2(value)
    if amount == amount.to_integral_value():
        return f"₹{int(amount):,}"
    return f"₹{amount:,.2f}"


def discount_percent(mrp: Decimal | int | float | str, price: Decimal | int | float | str) -> int:
    """Whole-number discount off MRP, rounded down so we never overstate savings."""
    mrp_d, price_d = to_decimal(mrp), to_decimal(price)
    if mrp_d <= 0 or price_d >= mrp_d:
        return 0
    return int(((mrp_d - price_d) * HUNDRED / mrp_d).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class PriceLine:
    key: str
    unit_price: Decimal
    unit_mrp: Decimal
    quantity: int
    tax_rate: Decimal = ZERO
    offer_discount: Decimal = ZERO  # whole-line offer discount from ``pureroots_pricing.price_lines``

    @property
    def line_original(self) -> Decimal:
        """Selling price × qty, before offers."""
        return q2(to_decimal(self.unit_price) * self.quantity)

    @property
    def line_total(self) -> Decimal:
        """Post-offer line total (``line_original − offer_discount``)."""
        return q2(self.line_original - q2(self.offer_discount))

    @property
    def line_mrp_total(self) -> Decimal:
        return q2(max(to_decimal(self.unit_mrp), to_decimal(self.unit_price)) * self.quantity)


@dataclass(frozen=True)
class CouponTerms:
    code: str
    discount_type: str  # "percent" | "flat"
    value: Decimal
    min_order_value: Decimal = ZERO
    max_discount: Decimal | None = None


@dataclass(frozen=True)
class ShippingRates:
    free_shipping_threshold: Decimal
    standard_fee: Decimal
    express_fee: Decimal


@dataclass(frozen=True)
class LineBreakdown:
    key: str
    line_total: Decimal
    discount_share: Decimal
    tax_included: Decimal


@dataclass(frozen=True)
class PriceBreakdown:
    mrp_total: Decimal
    items_total: Decimal
    offer_discount: Decimal
    subtotal: Decimal
    savings: Decimal
    coupon_discount: Decimal
    shipping: Decimal
    tax_included: Decimal
    total: Decimal
    free_shipping_threshold: Decimal
    amount_to_free_shipping: Decimal
    lines: tuple[LineBreakdown, ...]
    coins_discount: Decimal = ZERO


def calculate_subtotal(lines: Sequence[PriceLine]) -> Decimal:
    """Post-offer subtotal."""
    return q2(sum((line.line_total for line in lines), ZERO))


def calculate_items_total(lines: Sequence[PriceLine]) -> Decimal:
    """Σ selling price × qty (before offers)."""
    return q2(sum((line.line_original for line in lines), ZERO))


def calculate_mrp_total(lines: Sequence[PriceLine]) -> Decimal:
    return q2(sum((line.line_mrp_total for line in lines), ZERO))


def meets_minimum(subtotal: Decimal, coupon: CouponTerms) -> bool:
    return q2(subtotal) >= q2(coupon.min_order_value)


def calculate_coupon_discount(subtotal: Decimal, coupon: CouponTerms | None) -> Decimal:
    """Discount for an already-validated coupon; 0 when the minimum order is not met."""
    subtotal = q2(subtotal)
    if coupon is None or subtotal <= 0 or not meets_minimum(subtotal, coupon):
        return ZERO
    value = to_decimal(coupon.value)
    if coupon.discount_type == "percent":
        discount = q2(subtotal * value / HUNDRED)
        if coupon.max_discount is not None:
            discount = min(discount, q2(coupon.max_discount))
    elif coupon.discount_type == "flat":
        discount = q2(value)
    else:
        return ZERO
    return max(ZERO, min(discount, subtotal))


def calculate_shipping(
    subtotal: Decimal, coupon_discount: Decimal, delivery_method: str, rates: ShippingRates
) -> Decimal:
    if q2(subtotal) <= 0:
        return ZERO
    if delivery_method == "express":
        return q2(rates.express_fee)
    if q2(subtotal) - q2(coupon_discount) >= q2(rates.free_shipping_threshold):
        return ZERO
    return q2(rates.standard_fee)


def amount_to_free_shipping(subtotal: Decimal, coupon_discount: Decimal, rates: ShippingRates) -> Decimal:
    remaining = q2(rates.free_shipping_threshold) - (q2(subtotal) - q2(coupon_discount))
    return max(ZERO, q2(remaining))


def included_tax(amount: Decimal, tax_rate: Decimal) -> Decimal:
    """GST already included in ``amount``: ``amount - amount / (1 + rate/100)``."""
    amount, rate = to_decimal(amount), to_decimal(tax_rate)
    if amount <= 0 or rate <= 0:
        return ZERO
    return q2(amount - amount / (Decimal(1) + rate / HUNDRED))


def allocate_discount(line_totals: Sequence[Decimal], discount: Decimal) -> list[Decimal]:
    """Spread ``discount`` across lines proportionally; the last line absorbs rounding."""
    count = len(line_totals)
    total = sum(line_totals, ZERO)
    discount = q2(discount)
    if count == 0:
        return []
    if discount <= 0 or total <= 0:
        return [ZERO] * count
    shares: list[Decimal] = []
    allocated = ZERO
    for index, amount in enumerate(line_totals):
        if index == count - 1:
            share = discount - allocated
        else:
            share = min(q2(discount * amount / total), discount - allocated)
        allocated += share
        shares.append(share)
    return shares


def price_cart(
    lines: Sequence[PriceLine],
    coupon: CouponTerms | None,
    delivery_method: str,
    rates: ShippingRates,
    coins_discount: Decimal | int = ZERO,
) -> PriceBreakdown:
    """Full cart/checkout breakdown. ``coins_discount`` is the PureRoots Coins redemption in ₹."""
    items_total = calculate_items_total(lines)
    subtotal = calculate_subtotal(lines)
    mrp_total = calculate_mrp_total(lines)
    discount = calculate_coupon_discount(subtotal, coupon)
    shipping = calculate_shipping(subtotal, discount, delivery_method, rates)
    coins = max(ZERO, min(q2(coins_discount), q2(subtotal - discount)))

    line_totals = [line.line_total for line in lines]
    # Coupon and coins both reduce what is paid for the goods, so both are spread for the included GST.
    shares = allocate_discount(line_totals, discount + coins)
    breakdown_lines = tuple(
        LineBreakdown(
            key=line.key,
            line_total=line_total,
            discount_share=share,
            tax_included=included_tax(line_total - share, line.tax_rate),
        )
        for line, line_total, share in zip(lines, line_totals, shares, strict=True)
    )
    tax_total = q2(sum((item.tax_included for item in breakdown_lines), ZERO))

    return PriceBreakdown(
        mrp_total=mrp_total,
        items_total=items_total,
        offer_discount=q2(items_total - subtotal),
        subtotal=subtotal,
        savings=max(ZERO, q2(mrp_total - items_total)),
        coupon_discount=discount,
        shipping=shipping,
        tax_included=tax_total,
        total=q2(subtotal - discount - coins + shipping),
        free_shipping_threshold=q2(rates.free_shipping_threshold),
        amount_to_free_shipping=amount_to_free_shipping(subtotal, discount, rates),
        lines=breakdown_lines,
        coins_discount=coins,
    )
