"""Unit tests for the pure pricing functions (ARCHITECTURE §5 pricing rules)."""
from decimal import Decimal

import pytest

from app.services.pricing_service import (
    CouponTerms,
    PriceLine,
    ShippingRates,
    allocate_discount,
    amount_to_free_shipping,
    calculate_coupon_discount,
    calculate_shipping,
    calculate_subtotal,
    discount_percent,
    format_inr,
    included_tax,
    price_cart,
    q2,
)

RATES = ShippingRates(free_shipping_threshold=Decimal("999"), standard_fee=Decimal("79"), express_fee=Decimal("149"))
D = Decimal


def line(price: str, qty: int, mrp: str | None = None, tax: str = "5", key: str = "l") -> PriceLine:
    return PriceLine(key=key, unit_price=D(price), unit_mrp=D(mrp or price), quantity=qty, tax_rate=D(tax))


# ---- rounding ----


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2.345", "2.35"), ("2.344", "2.34"), ("0.005", "0.01"), ("10", "10.00"), ("199.999", "200.00")],
)
def test_q2_rounds_half_up(value: str, expected: str) -> None:
    assert q2(D(value)) == D(expected)


def test_q2_accepts_floats_without_binary_noise() -> None:
    assert q2(1.005) == D("1.01")


# ---- subtotal ----


def test_subtotal_is_sum_of_price_times_quantity() -> None:
    assert calculate_subtotal([line("199", 2), line("899", 1)]) == D("1297.00")


# ---- shipping ----


def test_standard_shipping_charged_below_threshold() -> None:
    assert calculate_shipping(D("998.99"), D("0"), "standard", RATES) == D("79.00")


def test_standard_shipping_free_at_threshold() -> None:
    assert calculate_shipping(D("999"), D("0"), "standard", RATES) == D("0.00")
    assert calculate_shipping(D("1500"), D("0"), "standard", RATES) == D("0.00")


def test_free_shipping_threshold_uses_amount_after_coupon() -> None:
    assert calculate_shipping(D("1050"), D("100"), "standard", RATES) == D("79.00")


def test_express_shipping_always_charged() -> None:
    assert calculate_shipping(D("5000"), D("0"), "express", RATES) == D("149.00")


def test_empty_cart_has_no_shipping() -> None:
    assert calculate_shipping(D("0"), D("0"), "standard", RATES) == D("0.00")
    assert calculate_shipping(D("0"), D("0"), "express", RATES) == D("0.00")


def test_amount_to_free_shipping() -> None:
    assert amount_to_free_shipping(D("800"), D("0"), RATES) == D("199.00")
    assert amount_to_free_shipping(D("1200"), D("0"), RATES) == D("0.00")


# ---- coupons ----


def test_percent_coupon_capped_by_max_discount() -> None:
    coupon = CouponTerms("BIG20", "percent", D("20"), max_discount=D("250"))
    assert calculate_coupon_discount(D("2000"), coupon) == D("250.00")


def test_percent_coupon_uncapped_rounds_half_up() -> None:
    coupon = CouponTerms("SAVE10", "percent", D("10"))
    assert calculate_coupon_discount(D("1234.55"), coupon) == D("123.46")


def test_flat_coupon() -> None:
    coupon = CouponTerms("FLAT150", "flat", D("150"))
    assert calculate_coupon_discount(D("600"), coupon) == D("150.00")


def test_flat_coupon_never_exceeds_subtotal() -> None:
    coupon = CouponTerms("FLAT500", "flat", D("500"))
    assert calculate_coupon_discount(D("300"), coupon) == D("300.00")


def test_coupon_requires_min_order_value() -> None:
    coupon = CouponTerms("MIN1000", "flat", D("100"), min_order_value=D("1000"))
    assert calculate_coupon_discount(D("999.99"), coupon) == D("0.00")
    assert calculate_coupon_discount(D("1000"), coupon) == D("100.00")


def test_no_coupon_means_no_discount() -> None:
    assert calculate_coupon_discount(D("500"), None) == D("0.00")


# ---- tax (prices include GST) ----


@pytest.mark.parametrize(
    ("amount", "rate", "expected"),
    [("105", "5", "5.00"), ("118", "18", "18.00"), ("112", "12", "12.00"), ("100", "0", "0.00"), ("199", "5", "9.48")],
)
def test_included_tax(amount: str, rate: str, expected: str) -> None:
    assert included_tax(D(amount), D(rate)) == D(expected)


def test_allocate_discount_is_proportional_and_exact() -> None:
    assert allocate_discount([D("600"), D("400")], D("100")) == [D("60.00"), D("40.00")]
    shares = allocate_discount([D("10"), D("10"), D("10")], D("10"))
    assert shares == [D("3.33"), D("3.33"), D("3.34")]
    assert sum(shares) == D("10.00")


def test_tax_included_after_spreading_coupon_across_lines() -> None:
    lines = [line("600", 1, tax="5", key="a"), line("400", 1, tax="12", key="b")]
    breakdown = price_cart(lines, CouponTerms("FLAT100", "flat", D("100")), "standard", RATES)
    # taxable 540 @5% -> 25.71 ; 360 @12% -> 38.57
    assert [item.tax_included for item in breakdown.lines] == [D("25.71"), D("38.57")]
    assert breakdown.tax_included == D("64.28")


# ---- full breakdown ----


def test_price_cart_full_breakdown() -> None:
    lines = [line("199", 2, mrp="240", key="a"), line("899", 1, mrp="1050", key="b")]
    coupon = CouponTerms("SAVE10", "percent", D("10"), max_discount=D("100"))
    breakdown = price_cart(lines, coupon, "standard", RATES)
    assert breakdown.subtotal == D("1297.00")
    assert breakdown.mrp_total == D("1530.00")
    assert breakdown.savings == D("233.00")
    assert breakdown.coupon_discount == D("100.00")
    assert breakdown.shipping == D("0.00")  # 1197 >= 999
    assert breakdown.total == D("1197.00")
    assert breakdown.amount_to_free_shipping == D("0.00")


def test_price_cart_total_includes_shipping_below_threshold() -> None:
    breakdown = price_cart([line("199", 2)], None, "standard", RATES)
    assert breakdown.subtotal == D("398.00")
    assert breakdown.shipping == D("79.00")
    assert breakdown.total == D("477.00")
    assert breakdown.amount_to_free_shipping == D("601.00")


def test_price_cart_express() -> None:
    breakdown = price_cart([line("1200", 1)], None, "express", RATES)
    assert breakdown.shipping == D("149.00")
    assert breakdown.total == D("1349.00")


def test_empty_cart_breakdown() -> None:
    breakdown = price_cart([], None, "standard", RATES)
    assert breakdown.total == D("0.00")
    assert breakdown.shipping == D("0.00")


# ---- display helpers ----


def test_discount_percent_rounds_down() -> None:
    assert discount_percent(D("240"), D("199")) == 17
    assert discount_percent(D("1050"), D("899")) == 14
    assert discount_percent(D("100"), D("100")) == 0
    assert discount_percent(D("0"), D("10")) == 0


def test_format_inr() -> None:
    assert format_inr(D("1000")) == "₹1,000"
    assert format_inr(D("199.5")) == "₹199.50"


# ---- offers (OFFERS_AND_ADMIN §3): line offer discounts come from pureroots_pricing ----


def test_price_cart_with_offer_discounts_coupon_on_post_offer_subtotal() -> None:
    lines = [
        PriceLine(
            key="a", unit_price=D("500"), unit_mrp=D("600"), quantity=2, tax_rate=D("5"), offer_discount=D("200")
        ),
        line("199", 1, mrp="240", key="b"),
    ]
    coupon = CouponTerms("COMBO10", "percent", D("10"))
    breakdown = price_cart(lines, coupon, "standard", RATES)
    assert breakdown.items_total == D("1199.00")
    assert breakdown.offer_discount == D("200.00")
    assert breakdown.subtotal == D("999.00")
    assert breakdown.mrp_total == D("1440.00")
    assert breakdown.savings == D("241.00")  # mrp_total - items_total
    assert breakdown.coupon_discount == D("99.90")  # 10% of 999, not of 1199
    assert breakdown.shipping == D("79.00")  # 999 - 99.90 < 999
    assert breakdown.total == D("978.10")
    assert [item.line_total for item in breakdown.lines] == [D("800.00"), D("199.00")]
