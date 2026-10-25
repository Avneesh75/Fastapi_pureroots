"""PureRoots Coins — the pure rule maths (no I/O). 1 coin = ₹1. Used by the cart, checkout and API.

EARN
    earn_base = subtotal (after offers) − coupon_discount − coins_redeemed      (shipping excluded)
    coins_to_earn = floor(earn_base × EARN_PERCENT / 100)
    Credited as *pending* when the order is placed, *available* when it is delivered.

REDEEM
    order_amount = subtotal (after offers) − coupon_discount                    (before coins and shipping)
    steps = min(floor(order_amount / STEP_AMOUNT), MAX_STEPS)
    max_redeemable = min(floor(balance × steps / MAX_STEPS), floor(order_amount))
    With the defaults (₹1,000 × 3): under ₹1,000 → 0, ₹1,000–1,999 → ⅓, ₹2,000–2,999 → ⅔, ₹3,000+ → all.
    A zero or negative balance redeems nothing.
"""
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class LoyaltyConfig:
    enabled: bool = True
    earn_percent: int = 30
    step_amount: Decimal = Decimal("1000")
    max_steps: int = 3

    @classmethod
    def from_settings(cls, settings: object) -> "LoyaltyConfig":
        return cls(
            enabled=bool(getattr(settings, "loyalty_enabled", True)),
            earn_percent=int(getattr(settings, "loyalty_earn_percent", 30)),
            step_amount=Decimal(str(getattr(settings, "loyalty_redeem_step_amount", "1000"))),
            max_steps=max(1, int(getattr(settings, "loyalty_redeem_max_steps", 3))),
        )

    @property
    def full_balance_amount(self) -> Decimal:
        """Order amount from which the whole balance can be used (₹3,000 by default)."""
        return self.step_amount * self.max_steps


def _dec(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _floor_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def inr(value: Decimal | int) -> str:
    """``₹1,000`` (whole rupees) or ``₹1,234.50``."""
    amount = _dec(value)
    if amount == amount.to_integral_value():
        return f"₹{int(amount):,}"
    return f"₹{amount:,.2f}"


def redeem_order_amount(subtotal: Decimal | int | float | str, coupon_discount: Decimal | int | float | str) -> Decimal:
    """The amount the redemption rule looks at: subtotal after offers and coupon, never below 0."""
    return max(ZERO, _dec(subtotal) - _dec(coupon_discount))


def redeem_steps(order_amount: Decimal | int | float | str, config: LoyaltyConfig) -> int:
    """Number of full steps (₹1,000 each by default), capped at ``max_steps``."""
    amount = _dec(order_amount)
    if amount <= 0 or config.step_amount <= 0:
        return 0
    return min(_floor_int(amount / config.step_amount), config.max_steps)


def fraction_label(steps: int, config: LoyaltyConfig) -> str:
    """``"0"``, ``"1/3"``, ``"2/3"`` or ``"all"``."""
    if steps <= 0:
        return "0"
    if steps >= config.max_steps:
        return "all"
    return f"{steps}/{config.max_steps}"


def max_redeemable(balance: int, order_amount: Decimal | int | float | str, config: LoyaltyConfig) -> int:
    """Most coins usable on an order of ``order_amount`` with ``balance`` available coins."""
    amount = _dec(order_amount)
    if not config.enabled or balance <= 0 or amount <= 0:
        return 0
    steps = redeem_steps(amount, config)
    if steps <= 0:
        return 0
    by_balance = (balance * steps) // config.max_steps
    return max(0, min(by_balance, _floor_int(amount)))


def clamp_redemption(requested: int, balance: int, order_amount: Decimal | int | float | str, config: LoyaltyConfig) -> int:
    """``requested`` clamped to what the rule allows (never negative)."""
    return max(0, min(int(requested), max_redeemable(balance, order_amount, config)))


def coins_to_earn(
    subtotal: Decimal | int | float | str,
    coupon_discount: Decimal | int | float | str,
    coins_redeemed: int,
    config: LoyaltyConfig,
) -> int:
    """Coins an order earns on delivery: floor(earn_percent % of what was paid for the goods)."""
    if not config.enabled or config.earn_percent <= 0:
        return 0
    base = _dec(subtotal) - _dec(coupon_discount) - Decimal(int(coins_redeemed))
    if base <= 0:
        return 0
    return max(0, _floor_int(base * config.earn_percent / Decimal(100)))


def amount_to_next_step(order_amount: Decimal | int | float | str, config: LoyaltyConfig) -> Decimal | None:
    """₹ still needed to unlock the next step, or ``None`` once the full balance is usable."""
    amount = _dec(order_amount)
    steps = redeem_steps(amount, config)
    if steps >= config.max_steps:
        return None
    return max(ZERO, config.step_amount * (steps + 1) - max(amount, ZERO))


def step_fraction_text(config: LoyaltyConfig) -> str:
    return "1/3" if config.max_steps == 3 else f"1/{config.max_steps}"


def rule_text(config: LoyaltyConfig) -> str:
    """Human summary of the redemption rule."""
    return (
        f"Use up to {step_fraction_text(config)} of your coins for every {inr(config.step_amount)} you spend — "
        f"all of them from {inr(config.full_balance_amount)}. 1 coin = ₹1."
    )


def blocked_reason(balance: int, order_amount: Decimal | int | float | str, config: LoyaltyConfig) -> str | None:
    """Why no coins can be used right now (``None`` when some can)."""
    if not config.enabled:
        return None
    if balance < 0:
        return "Your coin balance is below zero after a cancelled order, so coins can't be used until it's positive again."
    if balance == 0:
        return f"You don't have coins to use yet — you'll earn {config.earn_percent}% back as coins on delivered orders."
    if max_redeemable(balance, order_amount, config) > 0:
        return None
    needed = amount_to_next_step(order_amount, config)
    if needed is not None and needed > 0:
        return (
            f"Add {inr(needed.quantize(Decimal('0.01')))} more to use coins — you can use "
            f"{step_fraction_text(config)} of your coins for every {inr(config.step_amount)}."
        )
    return "Coins can't be used on this order."
