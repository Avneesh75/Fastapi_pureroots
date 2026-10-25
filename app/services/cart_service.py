"""Cart business logic.

* Guest carts are keyed by the ``pr_cart`` cookie (``carts.session_key``); user carts by ``user_id``.
* When a signed-in request still carries a guest key, the guest cart is merged into the user cart.
* Every read re-validates lines against live availability and clamps quantities, producing
  human-readable ``messages``.
* Offers (OFFERS_AND_ADMIN §3) are applied with ``pureroots_pricing.price_lines``; the coupon then
  runs on the post-offer subtotal. A coupon that is not combinable with offers is rejected with
  ``COUPON_NOT_COMBINABLE`` when applied, and dropped (with a message) when the cart is re-priced.
* PureRoots Coins (``loyalty_rules``): when a signed-in customer turns ``use_coins`` on, every re-price
  applies the most coins the rule allows for the current cart and balance (coupon first, then coins).
  If none can be used any more (the cart dropped under the first step, the balance changed) the toggle
  is turned off with a message.
"""
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from pureroots_pricing import LineInput, PricedLine, price_lines
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.clock import utcnow
from app.core.constants import LISTABLE_PRODUCT_STATUSES, MAX_LINE_QUANTITY
from app.core.exceptions import InsufficientStockError, NotFoundError, UnauthorizedError, ValidationAppError
from app.models.cart import Cart, CartItem
from app.models.catalog import Product, ProductVariant
from app.models.user import User
from app.repositories import cart_repository, catalog_repository
from app.schemas.cart import CartItemOut, CartOut, CartTotals, CartVariantOption
from app.services import coupon_service, loyalty_rules, pricing_service
from app.services.catalog_service import OfferContext, active_variants, is_sellable, offer_out, product_image_url
from app.services.coupon_service import CouponCheck
from app.services.pricing_service import PriceBreakdown, PriceLine, ShippingRates, money

ITEM_NOT_FOUND = "This item is no longer in your cart."
PRODUCT_UNAVAILABLE = "This product is no longer available."


@dataclass
class CartContext:
    """Who owns the cart for this request: a signed-in user and/or a guest session key."""

    user: User | None
    session_key: str | None

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.user.id if self.user is not None else None


@dataclass
class CartLine:
    item: CartItem
    variant: ProductVariant
    product: Product
    available: int  # 0 when the product is not sellable

    @property
    def in_stock(self) -> bool:
        return self.available > 0

    @property
    def name(self) -> str:
        return line_name(self.product, self.variant)


@dataclass
class CoinsState:
    """PureRoots Coins for one priced cart."""

    enabled: bool = False
    use: bool = False
    available: int = 0
    pending: int = 0
    max_redeemable: int = 0
    redeemed: int = 0
    to_earn: int = 0
    rule: str = ""
    fraction: str = "0"
    amount_to_next_step: Decimal | None = None
    blocked_reason: str | None = None


@dataclass
class PricedCart:
    cart: Cart | None
    lines: list[CartLine]
    breakdown: PriceBreakdown
    availability: dict[uuid.UUID, int] = field(default_factory=dict)
    coupon_check: CouponCheck | None = None
    messages: list[str] = field(default_factory=list)
    quantities_adjusted: bool = False
    offers: OfferContext | None = None
    offer_lines: dict[str, PricedLine] = field(default_factory=dict)  # by cart item id; in-stock lines only
    coins: CoinsState = field(default_factory=CoinsState)

    def offer_line(self, line: CartLine) -> PricedLine | None:
        return self.offer_lines.get(str(line.item.id))

    @property
    def purchasable(self) -> list[CartLine]:
        return [line for line in self.lines if line.in_stock]

    @property
    def out_of_stock(self) -> list[CartLine]:
        return [line for line in self.lines if not line.in_stock]


# --------------------------------------------------------------------------- helpers


def rates_from_settings(settings: Settings) -> ShippingRates:
    return ShippingRates(
        free_shipping_threshold=settings.free_shipping_threshold,
        standard_fee=settings.standard_shipping_fee,
        express_fee=settings.express_shipping_fee,
    )


def line_name(product: Product, variant: ProductVariant) -> str:
    return f"{product.name} {variant.label}".strip()


def price_line(line: CartLine, offer_discount: Decimal = pricing_service.ZERO) -> PriceLine:
    return PriceLine(
        key=str(line.item.id),
        unit_price=line.variant.price,
        unit_mrp=line.variant.mrp,
        quantity=line.item.quantity,
        tax_rate=line.product.tax_rate or pricing_service.ZERO,
        offer_discount=offer_discount,
    )


def apply_offers(lines: list[CartLine], offers: OfferContext) -> tuple[list[PriceLine], dict[str, PricedLine]]:
    """Price the in-stock lines with ``pureroots_pricing.price_lines`` (one offer per line, min order
    checked against the whole cart's items_total, per-offer caps) and wrap them as ``PriceLine``s."""
    in_stock = [line for line in lines if line.in_stock]
    result = price_lines(
        [
            LineInput(
                key=str(line.item.id),
                product_id=str(line.product.id),
                category_id=str(line.product.category_id),
                unit_price=Decimal(line.variant.price),
                quantity=line.item.quantity,
            )
            for line in in_stock
        ],
        offers.rules,
        offers.now,
    )
    by_key = {priced.key: priced for priced in result.lines}
    return [price_line(line, by_key[str(line.item.id)].line_discount) for line in in_stock], by_key


def _offer_totals(priced_lines: list[PriceLine]) -> tuple[Decimal, Decimal]:
    """``(post-offer subtotal, offer discount)``."""
    subtotal = pricing_service.calculate_subtotal(priced_lines)
    return subtotal, pricing_service.calculate_items_total(priced_lines) - subtotal


def _coupon_problem_message(check: CouponCheck) -> str:
    if check.not_combinable:
        return f"Coupon {check.code} was removed: it can't be combined with ongoing offers."
    return check.error or coupon_service.INVALID_CODE


def _clamp(desired: int, available: int, name: str) -> tuple[int, str | None]:
    """Clamp to stock and the per-line maximum, with a message when adjusted."""
    allowed = min(available, MAX_LINE_QUANTITY)
    if desired <= allowed:
        return desired, None
    if available <= MAX_LINE_QUANTITY:
        return allowed, f"{name}: only {available} left, quantity adjusted."
    return allowed, f"{name}: you can order up to {MAX_LINE_QUANTITY} per item, quantity adjusted."


def _sellable_variant(db: Session, variant_id: uuid.UUID) -> ProductVariant:
    variant = catalog_repository.get_variant(db, variant_id)
    if (
        variant is None
        or not variant.is_active
        or variant.product is None
        or variant.product.status not in LISTABLE_PRODUCT_STATUSES
    ):
        raise NotFoundError(PRODUCT_UNAVAILABLE)
    return variant


def _available(db: Session, variant: ProductVariant) -> int:
    if not is_sellable(variant.product):
        return 0
    return catalog_repository.get_availability(db, [variant.id]).get(variant.id, 0)


def _out_of_stock(variant: ProductVariant, requested: int) -> InsufficientStockError:
    return InsufficientStockError(
        f"Sorry, {line_name(variant.product, variant)} is out of stock.",
        details=[{"variant_id": str(variant.id), "sku": variant.sku, "requested": requested, "available": 0}],
    )


def _find_item(cart: Cart | None, item_id: uuid.UUID) -> CartItem:
    item = next((entry for entry in cart.items if entry.id == item_id), None) if cart else None
    if item is None:
        raise NotFoundError(ITEM_NOT_FOUND)
    return item


# --------------------------------------------------------------------------- cart lookup / merge


def _merge_into_user(db: Session, guest: Cart, user_id: uuid.UUID, user_cart: Cart | None) -> Cart:
    if user_cart is None:
        guest.user_id = user_id
        guest.session_key = None
        db.flush()
        return guest
    for guest_item in list(guest.items):
        existing = cart_repository.find_item(user_cart, guest_item.variant_id)
        if existing is not None:
            existing.quantity = min(existing.quantity + guest_item.quantity, MAX_LINE_QUANTITY)
        else:
            user_cart.items.append(
                CartItem(variant_id=guest_item.variant_id, quantity=min(guest_item.quantity, MAX_LINE_QUANTITY))
            )
    if not user_cart.coupon_code and guest.coupon_code:
        user_cart.coupon_code = guest.coupon_code
    db.delete(guest)
    db.flush()
    return user_cart


def merge_guest_cart(db: Session, user: User, session_key: str | None) -> None:
    """Merge the guest cart identified by ``session_key`` into the user's cart (login/register)."""
    if not session_key:
        return
    guest = cart_repository.get_by_session(db, session_key)
    if guest is None:
        return
    _merge_into_user(db, guest, user.id, cart_repository.get_by_user(db, user.id))
    db.commit()


def find_cart(db: Session, ctx: CartContext) -> Cart | None:
    if ctx.user is not None:
        cart = cart_repository.get_by_user(db, ctx.user.id)
        if ctx.session_key:
            guest = cart_repository.get_by_session(db, ctx.session_key)
            if guest is not None:
                cart = _merge_into_user(db, guest, ctx.user.id, cart)
                db.commit()
        return cart
    if ctx.session_key:
        return cart_repository.get_by_session(db, ctx.session_key)
    return None


def get_or_create_cart(db: Session, ctx: CartContext) -> Cart:
    cart = find_cart(db, ctx)
    if cart is not None:
        return cart
    if ctx.user is not None:
        return cart_repository.create(db, user_id=ctx.user.id)
    if ctx.session_key:
        return cart_repository.create(db, session_key=ctx.session_key)
    raise ValidationAppError("Your cart session could not be found. Please refresh the page.")


def clear_cart(cart: Cart) -> None:
    """Remove all lines and the coupon (caller commits)."""
    cart.items.clear()
    cart.coupon_code = None


# --------------------------------------------------------------------------- pricing / view


def load_lines(db: Session, cart: Cart) -> tuple[list[CartLine], dict[uuid.UUID, int], list[str], bool]:
    """Resolve cart items to variants, drop unavailable ones and clamp quantities to stock."""
    variants = catalog_repository.get_variants(db, [item.variant_id for item in cart.items])
    wanted_ids = set(variants)
    for variant in variants.values():
        if variant.product is not None:
            wanted_ids.update(sibling.id for sibling in variant.product.variants if sibling.is_active)
    availability = catalog_repository.get_availability(db, wanted_ids)

    lines: list[CartLine] = []
    messages: list[str] = []
    adjusted = False
    for item in list(cart.items):
        variant = variants.get(item.variant_id)
        product = variant.product if variant is not None else None
        if (
            variant is None
            or product is None
            or not variant.is_active
            or product.status not in LISTABLE_PRODUCT_STATUSES
        ):
            name = line_name(product, variant) if variant is not None and product is not None else "An item"
            messages.append(f"{name} is no longer available and was removed from your cart.")
            cart.items.remove(item)
            adjusted = True
            continue

        available = availability.get(variant.id, 0) if is_sellable(product) else 0
        name = line_name(product, variant)
        if available <= 0:
            messages.append(f"{name} is out of stock and is not included in your total.")
        else:
            quantity, message = _clamp(item.quantity, available, name)
            if message:
                item.quantity = quantity
                messages.append(message)
                adjusted = True
        lines.append(CartLine(item=item, variant=variant, product=product, available=available))
    return lines, availability, messages, adjusted


def price(db: Session, ctx: CartContext, cart: Cart | None, delivery_method: str, settings: Settings) -> PricedCart:
    """Re-price a cart on the server (used by the cart view and checkout)."""
    rates = rates_from_settings(settings)
    if cart is None:
        return PricedCart(cart=None, lines=[], breakdown=pricing_service.price_cart([], None, delivery_method, rates))

    lines, availability, messages, adjusted = load_lines(db, cart)
    offers = OfferContext.load(db)
    priced_lines, offer_lines = apply_offers(lines, offers)
    subtotal, offer_discount = _offer_totals(priced_lines)

    check: CouponCheck | None = None
    terms = None
    coupon_removed = False
    if cart.coupon_code and priced_lines:
        check = coupon_service.evaluate_coupon(
            db, cart.coupon_code, subtotal=subtotal, user_id=ctx.user_id, offer_discount=offer_discount
        )
        if check.is_valid:
            terms = check.terms
        else:
            messages.append(_coupon_problem_message(check))
            if not check.keep:
                cart.coupon_code = None
                coupon_removed = True

    coupon_discount = pricing_service.calculate_coupon_discount(subtotal, terms)
    coins, coins_removed = _coins_state(ctx, cart, subtotal, coupon_discount, bool(priced_lines), settings, messages)
    breakdown = pricing_service.price_cart(
        priced_lines, terms, delivery_method, rates, coins_discount=Decimal(coins.redeemed)
    )
    if adjusted or coupon_removed or coins_removed:
        db.commit()
    return PricedCart(
        cart=cart,
        lines=lines,
        breakdown=breakdown,
        availability=availability,
        coupon_check=check,
        messages=messages,
        quantities_adjusted=adjusted,
        offers=offers,
        offer_lines=offer_lines,
        coins=coins,
    )


def _coins_state(
    ctx: CartContext,
    cart: Cart | None,
    subtotal: Decimal,
    coupon_discount: Decimal,
    has_lines: bool,
    settings: Settings,
    messages: list[str],
) -> tuple[CoinsState, bool]:
    """Coins for this cart: the redemption is re-validated (and clamped) on every re-price."""
    cfg = loyalty_rules.LoyaltyConfig.from_settings(settings)
    if not cfg.enabled:
        return CoinsState(), False
    order_amount = loyalty_rules.redeem_order_amount(subtotal, coupon_discount)
    user = ctx.user
    balance = (user.coins_available or 0) if user is not None else 0
    max_redeemable = loyalty_rules.max_redeemable(balance, order_amount, cfg) if user is not None else 0
    use = bool(user is not None and cart is not None and cart.use_coins)
    removed = False
    if use and has_lines and max_redeemable <= 0:
        reason = loyalty_rules.blocked_reason(balance, order_amount, cfg) or "coins can't be used on this order."
        messages.append(f"Your coins were removed from this order. {reason}")
        assert cart is not None
        cart.use_coins = False
        use, removed = False, True
    redeemed = max_redeemable if use else 0
    steps = loyalty_rules.redeem_steps(order_amount, cfg)
    return (
        CoinsState(
            enabled=True,
            use=use,
            available=balance,
            pending=(user.coins_pending or 0) if user is not None else 0,
            max_redeemable=max_redeemable,
            redeemed=redeemed,
            to_earn=loyalty_rules.coins_to_earn(subtotal, coupon_discount, redeemed, cfg) if has_lines else 0,
            rule=loyalty_rules.rule_text(cfg),
            fraction=loyalty_rules.fraction_label(steps, cfg),
            amount_to_next_step=loyalty_rules.amount_to_next_step(order_amount, cfg),
            blocked_reason=(
                loyalty_rules.blocked_reason(balance, order_amount, cfg) if user is not None and has_lines else None
            ),
        ),
        removed,
    )


def build_view(priced: PricedCart, settings: Settings, extra_messages: list[str] | None = None) -> CartOut:
    items: list[CartItemOut] = []
    offers = priced.offers or OfferContext([], utcnow())
    for line in priced.lines:
        sellable = is_sellable(line.product)
        product_id, category_id = line.product.id, line.product.category_id
        options = [
            CartVariantOption(
                id=sibling.id,
                label=sibling.label,
                price=money(sibling.price),
                mrp=money(sibling.mrp),
                in_stock=sellable and priced.availability.get(sibling.id, 0) > 0,
                final_price=money(offers.price(sibling.price, product_id, category_id).final_price),
            )
            for sibling in active_variants(line.product)
        ]
        quantity = line.item.quantity
        offer_line = priced.offer_line(line)
        if offer_line is not None:
            unit_original, unit_final = offer_line.unit_original, offer_line.unit_final
            line_original, line_discount = offer_line.line_original, offer_line.line_discount
            line_final, offer = offer_line.line_final, offer_out(offer_line.offer, offer_line.label)
        else:
            # Out-of-stock line (excluded from totals): show the catalog display price.
            unit = offers.price(line.variant.price, product_id, category_id)
            unit_original, unit_final = unit.original_price, unit.final_price
            line_original = pricing_service.q2(unit.original_price * quantity)
            line_discount = pricing_service.q2(unit.discount * quantity)
            line_final, offer = line_original - line_discount, offer_out(unit.offer, unit.label)
        items.append(
            CartItemOut(
                id=line.item.id,
                variant_id=line.variant.id,
                product_id=line.product.id,
                product_slug=line.product.slug,
                product_name=line.product.name,
                variant_label=line.variant.label,
                sku=line.variant.sku,
                image_url=product_image_url(line.product, settings),
                unit_mrp=money(line.variant.mrp),
                unit_original_price=money(unit_original),
                unit_price=money(unit_final),
                quantity=quantity,
                line_original=money(line_original),
                line_discount=money(line_discount),
                line_total=money(line_final),
                offer=offer,
                available_quantity=line.available,
                in_stock=line.in_stock,
                variants=options,
            )
        )

    b = priced.breakdown
    totals = CartTotals(
        mrp_total=money(b.mrp_total),
        items_total=money(b.items_total),
        offer_discount=money(b.offer_discount),
        subtotal=money(b.subtotal),
        savings=money(b.savings),
        coupon_discount=money(b.coupon_discount),
        shipping=money(b.shipping),
        tax_included=money(b.tax_included),
        total=money(b.total),
        currency="INR",
        free_shipping_threshold=money(b.free_shipping_threshold),
        amount_to_free_shipping=money(b.amount_to_free_shipping),
        loyalty_enabled=priced.coins.enabled,
        coins_use=priced.coins.use,
        coins_available=priced.coins.available,
        coins_pending=priced.coins.pending,
        coins_max_redeemable=priced.coins.max_redeemable,
        coins_redeemed=priced.coins.redeemed,
        coins_discount=money(b.coins_discount),
        coins_to_earn=priced.coins.to_earn,
        coins_rule=priced.coins.rule,
        coins_redeem_fraction=priced.coins.fraction,
        coins_amount_to_next_step=(
            money(priced.coins.amount_to_next_step) if priced.coins.amount_to_next_step is not None else None
        ),
        coins_blocked_reason=priced.coins.blocked_reason,
    )
    cart = priced.cart
    return CartOut(
        id=cart.id if cart is not None else None,
        item_count=sum(line.item.quantity for line in priced.lines),
        coupon_code=cart.coupon_code if cart is not None else None,
        items=items,
        totals=totals,
        messages=[*(extra_messages or []), *priced.messages],
    )


def _view(
    db: Session,
    ctx: CartContext,
    cart: Cart | None,
    settings: Settings,
    *,
    delivery_method: str = "standard",
    messages: list[str] | None = None,
) -> CartOut:
    return build_view(price(db, ctx, cart, delivery_method, settings), settings, messages)


# --------------------------------------------------------------------------- use cases


def get_cart(db: Session, ctx: CartContext, delivery_method: str, settings: Settings) -> CartOut:
    return _view(db, ctx, find_cart(db, ctx), settings, delivery_method=delivery_method)


def add_item(db: Session, ctx: CartContext, variant_id: uuid.UUID, quantity: int, settings: Settings) -> CartOut:
    variant = _sellable_variant(db, variant_id)
    available = _available(db, variant)
    if available <= 0:
        raise _out_of_stock(variant, quantity)

    cart = get_or_create_cart(db, ctx)
    item = cart_repository.find_item(cart, variant.id)
    desired = (item.quantity if item is not None else 0) + quantity
    allowed, message = _clamp(desired, available, line_name(variant.product, variant))
    if item is not None:
        item.quantity = allowed
    else:
        cart.items.append(CartItem(variant_id=variant.id, quantity=allowed))
    db.commit()
    return _view(db, ctx, cart, settings, messages=[message] if message else None)


def update_item(
    db: Session,
    ctx: CartContext,
    item_id: uuid.UUID,
    settings: Settings,
    *,
    quantity: int | None = None,
    variant_id: uuid.UUID | None = None,
) -> CartOut:
    cart = find_cart(db, ctx)
    item = _find_item(cart, item_id)
    assert cart is not None
    requested = quantity if quantity is not None else item.quantity
    if requested <= 0:
        cart.items.remove(item)
        db.commit()
        return _view(db, ctx, cart, settings)

    message: str | None = None
    if variant_id is not None and variant_id != item.variant_id:
        target = _sellable_variant(db, variant_id)
        current = catalog_repository.get_variant(db, item.variant_id)
        if current is not None and current.product_id != target.product_id:
            error = "Please choose a size of the same product."
            raise ValidationAppError(error, details=[{"field": "variant_id", "message": error}])
        available = _available(db, target)
        if available <= 0:
            raise _out_of_stock(target, requested)
        other = cart_repository.find_item(cart, target.id)
        desired = requested + (other.quantity if other is not None else 0)
        new_quantity, message = _clamp(desired, available, line_name(target.product, target))
        if other is not None:
            other.quantity = new_quantity
            cart.items.remove(item)
        else:
            item.variant_id = target.id
            item.quantity = new_quantity
    else:
        variant = catalog_repository.get_variant(db, item.variant_id)
        available = _available(db, variant) if variant is not None and variant.is_active else 0
        if available > 0 and variant is not None:
            item.quantity, message = _clamp(requested, available, line_name(variant.product, variant))
        else:
            item.quantity = min(requested, MAX_LINE_QUANTITY)
    db.commit()
    return _view(db, ctx, cart, settings, messages=[message] if message else None)


def remove_item(db: Session, ctx: CartContext, item_id: uuid.UUID, settings: Settings) -> CartOut:
    cart = find_cart(db, ctx)
    item = _find_item(cart, item_id)
    assert cart is not None
    cart.items.remove(item)
    db.commit()
    return _view(db, ctx, cart, settings)


def apply_coupon(db: Session, ctx: CartContext, code: str, settings: Settings) -> CartOut:
    cart = find_cart(db, ctx)
    if cart is None or not cart.items:
        error = "Add something to your cart before applying a coupon."
        raise ValidationAppError(error, details=[{"field": "code", "message": error}])
    lines, _, _, _ = load_lines(db, cart)
    priced_lines, _ = apply_offers(lines, OfferContext.load(db))
    subtotal, offer_discount = _offer_totals(priced_lines)
    check = coupon_service.require_valid(
        coupon_service.evaluate_coupon(db, code, subtotal=subtotal, user_id=ctx.user_id, offer_discount=offer_discount)
    )
    cart.coupon_code = check.code
    db.commit()
    return _view(db, ctx, cart, settings, messages=[f"Coupon {check.code} applied."])


def remove_coupon(db: Session, ctx: CartContext, settings: Settings) -> CartOut:
    cart = find_cart(db, ctx)
    if cart is not None and cart.coupon_code:
        cart.coupon_code = None
        db.commit()
    return _view(db, ctx, cart, settings)


def set_use_coins(db: Session, ctx: CartContext, use: bool, settings: Settings) -> CartOut:
    """Turn "use my coins" on or off (signed-in customers only)."""
    if ctx.user is None:
        raise UnauthorizedError("Sign in to use your PureRoots Coins.")
    if not loyalty_rules.LoyaltyConfig.from_settings(settings).enabled:
        raise ValidationAppError("PureRoots Coins are not available right now.")
    cart = find_cart(db, ctx)
    if use and (cart is None or not cart.items):
        error = "Add something to your cart before using coins."
        raise ValidationAppError(error, details=[{"field": "use", "message": error}])
    if cart is None:
        return _view(db, ctx, cart, settings)
    if not use:
        cart.use_coins = False
        db.commit()
        return _view(db, ctx, cart, settings, messages=["Coins won't be used on this order."])
    cart.use_coins = True
    priced = price(db, ctx, cart, "standard", settings)
    if priced.coins.redeemed <= 0:
        db.rollback()
        error = priced.coins.blocked_reason or "Coins can't be used on this order."
        raise ValidationAppError(error, details=[{"field": "use", "message": error}])
    db.commit()
    count = priced.coins.redeemed
    note = f"Using {count:,} coin{'s' if count != 1 else ''} (saving ₹{count:,})."
    return build_view(priced, settings, [note])
