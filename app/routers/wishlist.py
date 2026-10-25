"""/api/v1/wishlist — saved products (sign-in required)."""
import uuid
from typing import Any

from fastapi import APIRouter

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CurrentUser, DbSession, SettingsDep
from app.schemas.catalog import ProductCard
from app.schemas.wishlist import WishlistAdd
from app.services import wishlist_service

router = APIRouter(prefix="/wishlist", tags=["Wishlist"])


@router.get(
    "",
    response_model=ApiResponse[list[ProductCard]],
    summary="List wishlisted products",
    responses=error_responses(401),
)
def list_wishlist(user: CurrentUser, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(wishlist_service.list_cards(db, user, settings))


@router.post(
    "",
    response_model=ApiResponse[list[ProductCard]],
    summary="Add a product to the wishlist",
    description="Idempotent. Returns the updated wishlist.",
    responses=error_responses(401, 403, 404, 422),
)
def add_to_wishlist(payload: WishlistAdd, user: CurrentUser, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    cards = wishlist_service.add(db, user, payload.product_id, settings)
    return envelope(cards, message="Saved to your wishlist.")


@router.delete(
    "/{product_id}",
    response_model=ApiResponse[list[ProductCard]],
    summary="Remove a product from the wishlist",
    description="Idempotent. Returns the updated wishlist.",
    responses=error_responses(401, 403),
)
def remove_from_wishlist(
    product_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    cards = wishlist_service.remove(db, user, product_id, settings)
    return envelope(cards, message="Removed from your wishlist.")
