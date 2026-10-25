"""Offer maths. The binding rules live in docs/OFFERS_AND_ADMIN.md §1.

All money is ``Decimal`` rounded ROUND_HALF_UP to 2dp. Nothing here touches a database,
a clock or a framework: callers pass ``now`` and a list of ``OfferRule``.

Notes on edge cases the contract leaves open (both services get them from here):

* A ``min_order_amount`` of ``None`` **or 0** means "no minimum": such an offer is
  unconditional and is applied by ``price_unit``. Only a minimum > 0 makes it conditional.
* The tie-break "earlier ``starts_at``" treats ``None`` (starts immediately) as earliest.
* ``max_discount_amount`` caps the per-unit discount (used for ranking and for
  ``price_unit``) and, in ``price_lines``, also the offer's total across the cart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

PERCENT = "percent"
FLAT = "flat"
DISCOUNT_TYPES = (PERCENT, FLAT)

SCOPE_ALL = "all"
SCOPE_CATEGORY = "category"
SCOPE_PRODUCTS = "products"
SCOPES = (SCOPE_ALL, SCOPE_CATEGORY, SCOPE_PRODUCTS)

MAX_PERCENT = Decimal("90")

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def _money(value: Any) -> Decimal:
    """Coerce to Decimal and round half-up to 2dp."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(value)


def _fmt_number(value: Decimal) -> str:
    """20 -> '20', 12.5 -> '12.5', 99.50 -> '99.50' (money keeps 2dp when fractional)."""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def format_inr(amount: Any) -> str:
    """Indian digit grouping: 1499 -> '₹1,499', 150000 -> '₹1,50,000', 99.5 -> '₹99.50'."""
    amount = _money(amount)
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    frac = amount - whole
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    text = digits if frac == 0 else f"{digits}.{str(frac.quantize(_CENT))[2:]}"
    return f"{'-' if negative else ''}₹{text}"


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class OfferRule:
    id: str
    name: str
    discount_type: str  # "percent" | "flat"
    value: Decimal
    scope: str  # "all" | "category" | "products"
    category_ids: frozenset[str]
    product_ids: frozenset[str]
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    min_order_amount: Decimal | None = None
    max_discount_amount: Decimal | None = None
    priority: int = 0

    def is_live(self, now: datetime) -> bool:
        if not self.is_active:
            return False
        if self.starts_at is not None and self.starts_at > now:
            return False
        if self.ends_at is not None and not (now < self.ends_at):
            return False
        return True

    def applies_to(self, product_id: str, category_id: str) -> bool:
        if self.scope == SCOPE_ALL:
            return True
        if self.scope == SCOPE_CATEGORY:
            return category_id is not None and str(category_id) in self.category_ids
        if self.scope == SCOPE_PRODUCTS:
            return product_id is not None and str(product_id) in self.product_ids
        return False

    # -- helpers (not part of the contract surface, but harmless)
    @property
    def is_conditional(self) -> bool:
        return self.min_order_amount is not None and Decimal(self.min_order_amount) > 0

    def unit_discount(self, price: Any) -> Decimal | None:
        """Per-unit discount for ``price`` or ``None`` when the offer can't apply."""
        price = _money(price)
        if price <= 0:
            return None
        value = Decimal(self.value)
        if self.discount_type == PERCENT:
            discount = _money(price * value / Decimal(100))
        elif self.discount_type == FLAT:
            if price <= value:
                return None  # never price a unit at zero or below
            discount = _money(value)
        else:
            return None
        if self.max_discount_amount is not None:
            discount = min(discount, _money(self.max_discount_amount))
        # Rounding must never make a unit free: leave at least one paisa.
        discount = min(discount, price - _CENT)
        if discount <= 0:
            return None
        return discount


@dataclass(frozen=True)
class PricedUnit:
    original_price: Decimal
    discount: Decimal
    final_price: Decimal
    discount_percent: int
    offer: OfferRule | None
    label: str | None


@dataclass(frozen=True)
class LineInput:
    key: str
    product_id: str
    category_id: str
    unit_price: Decimal
    quantity: int


@dataclass(frozen=True)
class PricedLine:
    key: str
    unit_original: Decimal
    unit_final: Decimal
    line_original: Decimal
    line_discount: Decimal
    line_final: Decimal
    offer: OfferRule | None
    label: str | None


@dataclass(frozen=True)
class CartOfferResult:
    lines: list[PricedLine]
    items_total: Decimal
    offer_discount: Decimal
    subtotal: Decimal


# --------------------------------------------------------------------------- labels


def offer_label(rule: OfferRule) -> str:
    """'20% OFF' for percent offers, '₹100 OFF' for flat ones."""
    value = Decimal(rule.value)
    if rule.discount_type == PERCENT:
        return f"{_fmt_number(value)}% OFF"
    return f"{format_inr(value)} OFF"


def offer_hint(rule: OfferRule) -> str:
    """Hint for a conditional offer: 'Extra 10% OFF on orders above ₹1,499'."""
    text = f"Extra {offer_label(rule)}"
    if rule.is_conditional:
        text += f" on orders above {format_inr(rule.min_order_amount)}"
    return text


# --------------------------------------------------------------------------- selection


def _rank_key(discount: Decimal, rule: OfferRule):
    starts = (0,) if rule.starts_at is None else (1, rule.starts_at)
    return (-discount, -int(rule.priority or 0), starts, str(rule.id))


def _best(price: Decimal, candidates: Iterable[OfferRule]) -> tuple[OfferRule | None, Decimal]:
    best: tuple | None = None
    for rule in candidates:
        discount = rule.unit_discount(price)
        if discount is None:
            continue
        key = _rank_key(discount, rule)
        if best is None or key < best[0]:
            best = (key, rule, discount)
    if best is None:
        return None, _ZERO
    return best[1], best[2]


def _percent_of(discount: Decimal, original: Decimal) -> int:
    if original <= 0 or discount <= 0:
        return 0
    return int((discount / original * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price_unit(price, product_id, category_id, offers: Sequence[OfferRule], now: datetime) -> PricedUnit:
    """Catalog display price for one variant. Conditional (min-order) offers are skipped."""
    original = _money(price)
    candidates = (
        r
        for r in offers
        if r.is_live(now) and not r.is_conditional and r.applies_to(product_id, category_id)
    )
    rule, discount = _best(original, candidates)
    return PricedUnit(
        original_price=original,
        discount=discount,
        final_price=original - discount,
        discount_percent=_percent_of(discount, original),
        offer=rule,
        label=offer_label(rule) if rule else None,
    )


def conditional_offers(product_id, category_id, offers: Sequence[OfferRule], now: datetime) -> list[OfferRule]:
    """Live offers for this product that need a minimum order (for hints)."""
    found = [
        r
        for r in offers
        if r.is_live(now) and r.is_conditional and r.applies_to(product_id, category_id)
    ]
    found.sort(key=lambda r: (Decimal(r.min_order_amount), -int(r.priority or 0), str(r.id)))
    return found


def price_lines(lines: Sequence[LineInput], offers: Sequence[OfferRule], now: datetime) -> CartOfferResult:
    """Authoritative cart pricing: one offer per line, min-order vs items_total, cart-level caps."""
    prepared = []
    for line in lines:
        unit = _money(line.unit_price)
        qty = max(int(line.quantity), 0)
        prepared.append((line, unit, qty, _money(unit * qty)))
    items_total = _money(sum((p[3] for p in prepared), _ZERO))

    live = [r for r in offers if r.is_live(now)]
    eligible = [
        r for r in live if not r.is_conditional or items_total >= _money(r.min_order_amount)
    ]

    chosen: list[tuple[OfferRule | None, Decimal]] = []
    for line, unit, qty, _orig in prepared:
        if qty == 0:
            chosen.append((None, _ZERO))
            continue
        rule, unit_disc = _best(
            unit, (r for r in eligible if r.applies_to(line.product_id, line.category_id))
        )
        chosen.append((rule, _money(unit_disc * qty)))

    # Cart-level cap per offer: spread proportionally to line_original, last line absorbs rounding.
    discounts = [d for _, d in chosen]
    by_offer: dict[str, list[int]] = {}
    for idx, (rule, _d) in enumerate(chosen):
        if rule is not None:
            by_offer.setdefault(str(rule.id), []).append(idx)
    for idxs in by_offer.values():
        rule = chosen[idxs[0]][0]
        if rule.max_discount_amount is None:
            continue
        cap = _money(rule.max_discount_amount)
        total = sum((discounts[i] for i in idxs), _ZERO)
        if total <= cap:
            continue
        base = sum((prepared[i][3] for i in idxs), _ZERO)
        allocated = _ZERO
        for n, i in enumerate(idxs):
            if n == len(idxs) - 1:
                share = cap - allocated
            else:
                share = _money(cap * prepared[i][3] / base)
                allocated += share
            discounts[i] = share

    priced: list[PricedLine] = []
    for (line, unit, qty, orig), (rule, _d), disc in zip(prepared, chosen, discounts):
        if qty and disc:
            unit_final = _money(unit - disc / qty)
        else:
            unit_final = unit
            if not disc:
                rule = None
        priced.append(
            PricedLine(
                key=line.key,
                unit_original=unit,
                unit_final=unit_final,
                line_original=orig,
                line_discount=disc,
                line_final=orig - disc,
                offer=rule,
                label=offer_label(rule) if rule else None,
            )
        )
    offer_discount = _money(sum((p.line_discount for p in priced), _ZERO))
    return CartOfferResult(
        lines=priced,
        items_total=items_total,
        offer_discount=offer_discount,
        subtotal=items_total - offer_discount,
    )


# --------------------------------------------------------------------------- validation


def validate_offer(**fields) -> dict[str, str]:
    """Validate offer fields. Returns {field: message}; {} means valid.

    Accepted keywords (all optional, missing = not provided): name, discount_type, value, scope,
    category_ids, product_ids, starts_at, ends_at, min_order_amount, max_discount_amount.
    Error keys use the same names.
    """
    errors: dict[str, str] = {}

    name = fields.get("name")
    if name is None or not str(name).strip():
        errors["name"] = "Name is required."

    discount_type = fields.get("discount_type")
    if discount_type not in DISCOUNT_TYPES:
        errors["discount_type"] = "Choose percentage or flat amount."

    def number(key: str) -> Decimal | None:
        try:
            return _dec(fields.get(key))
        except ValueError:
            errors[key] = "Enter a valid number."
            return None

    value = number("value")
    min_order = number("min_order_amount")
    max_discount = number("max_discount_amount")

    if "value" not in errors:
        if value is None:
            errors["value"] = "Value is required."
        elif discount_type == PERCENT and not (Decimal(0) < value <= MAX_PERCENT):
            errors["value"] = "A percentage must be more than 0 and at most 90."
        elif discount_type == FLAT and not value > 0:
            errors["value"] = "A flat amount must be more than 0."

    starts_at = fields.get("starts_at")
    ends_at = fields.get("ends_at")
    if starts_at is not None and ends_at is not None and not ends_at > starts_at:
        errors["ends_at"] = "End must be after the start."

    scope = fields.get("scope")
    if scope not in SCOPES:
        errors["scope"] = "Choose all products, a category or selected products."
    elif scope == SCOPE_CATEGORY and not fields.get("category_ids"):
        errors["category_ids"] = "Pick at least one category."
    elif scope == SCOPE_PRODUCTS and not fields.get("product_ids"):
        errors["product_ids"] = "Pick at least one product."

    if "min_order_amount" not in errors and min_order is not None and min_order < 0:
        errors["min_order_amount"] = "Minimum order can't be negative."
    if "max_discount_amount" not in errors and max_discount is not None:
        if max_discount < 0:
            errors["max_discount_amount"] = "Maximum discount can't be negative."
        elif discount_type == FLAT:
            errors["max_discount_amount"] = "A maximum discount only applies to percentage offers."

    if (
        discount_type == FLAT
        and "value" not in errors
        and "min_order_amount" not in errors
        and min_order is not None
        and min_order > 0
        and not value < min_order
    ):
        errors["value"] = "A flat amount must be less than the minimum order."

    return errors
