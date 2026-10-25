"""/api/v1/categories — active categories with product counts."""
from typing import Any

from fastapi import APIRouter

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession, SettingsDep
from app.schemas.catalog import CategoryOut
from app.services import catalog_service

router = APIRouter(prefix="/categories", tags=["Catalog"])


@router.get(
    "",
    response_model=ApiResponse[list[CategoryOut]],
    summary="List active categories",
    description="Ordered by `sort_order`, each with `product_count` (active + out-of-stock products).",
)
def list_categories(db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(catalog_service.list_categories(db, settings))


@router.get(
    "/{slug}",
    response_model=ApiResponse[CategoryOut],
    summary="Get a category by slug",
    responses=error_responses(404),
)
def get_category(slug: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(catalog_service.get_category(db, slug, settings))
