"""PureRoots Coins: the pure rule maths (loyalty_rules) and the coins discount in price_cart."""
from decimal import Decimal as D

import pytest

from app.services.loyalty_rules import (
    LoyaltyConfig,
    amount_to_next_step,
    blocked_reason,
    clamp_redemption,
    coins_to_earn,
    fraction_label,
    max_redeemable,
    redeem_order_amount,
    redeem_steps,
    rule_text,
)
from app.services.pricing_service import PriceLine, ShippingRates, price_cart

CFG = LoyaltyConfig()
RATES = ShippingRates(free_shipping_threshold=D("999"), standard_fee=D("79"), express_fee=D("149"))


@pytest.mark.parametrize(
    ("amount", "steps", "fraction", "coins"),
    [
        ("0", 0, "0", 0),
        ("999", 0, "0", 0),
        ("999.99", 0, "0", 0),
        ("1000", 1, "1/3", 200),
        ("1500", 1, "1/3", 200),
        ("1999", 1, "1/3", 200),
        ("1999.99", 1, "1/3", 200),
        ("2000", 2, "2/3", 400),
        ("2999", 2, "2/3", 400),
        ("3000", 3, "all", 600),
        ("5000", 3, "all", 600),
    ],
)
def test_boundaries_with_600_coins(amount: str, steps: int, fraction: str, coins: int) -> None:
    assert redeem_steps(D(amount), CFG) == steps
    assert fraction_label(steps, CFG) == fraction
    assert max_redeemable(600, D(amount), CFG) == coins


def test_balance_fraction_is_floored() -> None:
    assert max_redeemable(100, D("1000"), CFG) == 33  # floor(100 / 3)
    assert max_redeemable(100, D("2000"), CFG) == 66  # floor(200 / 3)
    assert max_redeemable(1, D("1500"), CFG) == 0
    assert max_redeemable(1, D("3000"), CFG) == 1


def test_capped_by_balance_and_by_order_amount() -> None:
    assert max_redeemable(90, D("1000"), CFG) == 30  # balance cap
    assert max_redeemable(10_000, D("3000"), CFG) == 3000  # subtotal cap: goods can't go negative
    assert max_redeemable(10_000, D("3500.50"), CFG) == 3500
    assert max_redeemable(9_000, D("1200"), CFG) == 1200  # 1/3 of 9000 = 3000 > 1200


@pytest.mark.parametrize("balance", [0, -1, -500])
def test_zero_or_negative_balance_redeems_nothing(balance: int) -> None:
    assert max_redeemable(balance, D("5000"), CFG) == 0
    assert clamp_redemption(100, balance, D("5000"), CFG) == 0


def test_clamp_redemption() -> None:
    assert clamp_redemption(500, 600, D("1500"), CFG) == 200
    assert clamp_redemption(150, 600, D("1500"), CFG) == 150
    assert clamp_redemption(-5, 600, D("1500"), CFG) == 0


def test_disabled_config_redeems_and_earns_nothing() -> None:
    off = LoyaltyConfig(enabled=False)
    assert max_redeemable(600, D("5000"), off) == 0
    assert coins_to_earn(D("5000"), 0, 0, off) == 0
    assert blocked_reason(600, D("5000"), off) is None


def test_custom_steps() -> None:
    cfg = LoyaltyConfig(step_amount=D("500"), max_steps=4)
    assert max_redeemable(400, D("499"), cfg) == 0
    assert max_redeemable(400, D("500"), cfg) == 100
    assert max_redeemable(400, D("1500"), cfg) == 300
    assert max_redeemable(400, D("2000"), cfg) == 400
    assert fraction_label(3, cfg) == "3/4"
    assert "1/4 of your coins for every ₹500" in rule_text(cfg)


def test_redeem_order_amount_is_after_coupon_and_never_negative() -> None:
    assert redeem_order_amount(D("1500"), D("150")) == D("1350")
    assert redeem_order_amount(D("100"), D("150")) == D("0")


def test_coins_to_earn() -> None:
    # 30% of (subtotal − coupon − coins), floored; shipping is never part of it.
    assert coins_to_earn(D("1500"), D("100"), 200, CFG) == 360
    assert coins_to_earn(D("1001"), 0, 0, CFG) == 300  # 300.3 → 300
    assert coins_to_earn(D("3.33"), 0, 0, CFG) == 0
    assert coins_to_earn(D("500"), D("200"), 300, CFG) == 0
    assert coins_to_earn(D("500"), D("200"), 400, CFG) == 0  # never negative
    assert coins_to_earn(D("1000"), 0, 0, LoyaltyConfig(earn_percent=10)) == 100


def test_amount_to_next_step() -> None:
    assert amount_to_next_step(D("0"), CFG) == D("1000")
    assert amount_to_next_step(D("750"), CFG) == D("250")
    assert amount_to_next_step(D("1500"), CFG) == D("500")
    assert amount_to_next_step(D("2999"), CFG) == D("1")
    assert amount_to_next_step(D("3000"), CFG) is None


def test_rule_and_blocked_texts() -> None:
    assert rule_text(CFG) == (
        "Use up to 1/3 of your coins for every ₹1,000 you spend — all of them from ₹3,000. 1 coin = ₹1."
    )
    assert blocked_reason(600, D("750"), CFG) == (
        "Add ₹250 more to use coins — you can use 1/3 of your coins for every ₹1,000."
    )
    assert blocked_reason(600, D("1500"), CFG) is None
    assert "don't have coins" in (blocked_reason(0, D("1500"), CFG) or "")
    assert "below zero" in (blocked_reason(-20, D("1500"), CFG) or "")
    # 1 coin at ₹1,500: floor(1/3) = 0 — explain how to unlock more.
    assert "Add ₹500 more" in (blocked_reason(1, D("1500"), CFG) or "")


def _line(price: str, qty: int, tax: str = "5") -> PriceLine:
    return PriceLine(key=f"{price}x{qty}", unit_price=D(price), unit_mrp=D(price), quantity=qty, tax_rate=D(tax))


def test_price_cart_applies_coins_after_coupon_and_spreads_them_for_gst() -> None:
    from app.services.pricing_service import CouponTerms

    lines = [_line("1000", 1, "5"), _line("500", 1, "12")]
    coupon = CouponTerms("FLAT150", "flat", D("150"))
    breakdown = price_cart(lines, coupon, "standard", RATES, coins_discount=D("200"))
    assert breakdown.subtotal == D("1500.00")
    assert breakdown.coupon_discount == D("150.00")
    assert breakdown.coins_discount == D("200.00")
    assert breakdown.shipping == D("0.00")  # free-shipping check uses subtotal − coupon (coins ignored)
    assert breakdown.total == D("1150.00")
    # 350 spread 2:1 → 233.33 / 116.67
    assert [line.discount_share for line in breakdown.lines] == [D("233.33"), D("116.67")]
    assert breakdown.tax_included == D("36.51") + D("41.07")  # (1000−233.33)×5/105 + (500−116.67)×12/112


def test_price_cart_caps_coins_at_goods_and_keeps_shipping() -> None:
    breakdown = price_cart([_line("400", 1)], None, "standard", RATES, coins_discount=D("1000"))
    assert breakdown.coins_discount == D("400.00")
    assert breakdown.shipping == D("79.00")
    assert breakdown.total == D("79.00")
    assert breakdown.tax_included == D("0.00")


def test_price_cart_without_coins_is_unchanged() -> None:
    breakdown = price_cart([_line("199", 2)], None, "standard", RATES)
    assert breakdown.coins_discount == D("0")
    assert breakdown.total == D("477.00")
