"""/api/v1/cart — guest (``pr_cart`` cookie) or user cart."""
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CartCtx, DbSession, SettingsDep
from app.schemas.cart import CartItemCreate, CartItemUpdate, CartOut, CouponApply
from app.schemas.loyalty import CartCoinsToggle
from app.services import cart_service

router = APIRouter(prefix="/cart", tags=["Cart"])

CART_ERRORS = error_responses(401, 403, 404, 409, 422)


@router.get(
    "",
    response_model=ApiResponse[CartOut],
    summary="Get the cart",
    description=(
        "Re-validates every line against live stock (quantities are clamped with a message in `messages`). "
        "`delivery_method=express` prices express shipping."
    ),
    responses=error_responses(401),
)
def get_cart(
    ctx: CartCtx,
    db: DbSession,
    settings: SettingsDep,
    delivery_method: Literal["standard", "express"] = Query("standard"),
) -> dict[str, Any]:
    return envelope(cart_service.get_cart(db, ctx, delivery_method, settings))


@router.post(
    "/items",
    response_model=ApiResponse[CartOut],
    summary="Add a variant to the cart",
    description="Adds to an existing line if present. Quantity is clamped to available stock (max 20 per line).",
    responses=CART_ERRORS,
)
def add_item(payload: CartItemCreate, ctx: CartCtx, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    cart = cart_service.add_item(db, ctx, payload.variant_id, payload.quantity, settings)
    return envelope(cart, message="Added to your cart.")


@router.patch(
    "/items/{item_id}",
    response_model=ApiResponse[CartOut],
    summary="Change quantity and/or size of a line",
    description="`quantity: 0` removes the line. Changing `variant_id` must stay within the same product.",
    responses=CART_ERRORS,
)
def update_item(
    item_id: uuid.UUID, payload: CartItemUpdate, ctx: CartCtx, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    cart = cart_service.update_item(
        db, ctx, item_id, settings, quantity=payload.quantity, variant_id=payload.variant_id
    )
    return envelope(cart)


@router.delete(
    "/items/{item_id}",
    response_model=ApiResponse[CartOut],
    summary="Remove a line",
    responses=CART_ERRORS,
)
def remove_item(item_id: uuid.UUID, ctx: CartCtx, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(cart_service.remove_item(db, ctx, item_id, settings), message="Removed from your cart.")


@router.post(
    "/coupon",
    response_model=ApiResponse[CartOut],
    summary="Apply a coupon",
    description="Validates activity, dates, usage limits and minimum order value.",
    responses=CART_ERRORS,
)
def apply_coupon(payload: CouponApply, ctx: CartCtx, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(cart_service.apply_coupon(db, ctx, payload.code, settings))


@router.delete(
    "/coupon",
    response_model=ApiResponse[CartOut],
    summary="Remove the coupon",
    responses=error_responses(401, 403),
)
def remove_coupon(ctx: CartCtx, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(cart_service.remove_coupon(db, ctx, settings), message="Coupon removed.")


@router.post(
    "/coins",
    response_model=ApiResponse[CartOut],
    summary="Use (or stop using) PureRoots Coins on this cart",
    description=(
        "Signed-in customers only (401 for guests). `{use: true}` applies the most coins the rule allows: "
        "1/3 of the available balance for every full ₹1,000 of the subtotal after offers and coupon, the whole "
        "balance from ₹3,000, never more than that amount (shipping is always paid). The redemption is "
        "re-validated on every re-price. `use: true` when nothing can be used returns 422 with the reason."
    ),
    responses=error_responses(401, 403, 422),
)
def set_coins(payload: CartCoinsToggle, ctx: CartCtx, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(cart_service.set_use_coins(db, ctx, payload.use, settings))
