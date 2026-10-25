"""/api/v1/search — full search (same shape as /products) and type-ahead suggestions."""
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.pagination import PageParams, page_meta, page_params
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import DbSession, SettingsDep
from app.repositories.catalog_repository import ProductFilters
from app.routers.products import common_filter_params
from app.schemas.catalog import ProductCard, SuggestionsOut
from app.services import catalog_service, search_service

router = APIRouter(prefix="/search", tags=["Search"])


@router.get(
    "",
    response_model=ApiResponse[list[ProductCard]],
    summary="Search products",
    description=(
        "Same parameters and response as `GET /products`, but `q` is required. Matches name, short "
        "description, tags, keywords and category name (case-insensitive; every word must match)."
    ),
    responses=error_responses(422),
)
def search_products(
    db: DbSession,
    settings: SettingsDep,
    q: str = Query(..., min_length=1, max_length=100),
    filters: ProductFilters = Depends(common_filter_params),
    page: PageParams = Depends(page_params),
) -> dict[str, Any]:
    filters.q = q.strip() or None
    cards, total = catalog_service.list_products(db, filters, page.offset, page.page_size, settings)
    return envelope(cards, meta=page_meta(page.page, page.page_size, total))


@router.get(
    "/suggestions",
    response_model=ApiResponse[SuggestionsOut],
    summary="Type-ahead suggestions",
    description="Up to 6 products, 4 categories and 6 keywords. Queries shorter than 2 characters return empty lists.",
)
def suggestions(db: DbSession, settings: SettingsDep, q: str = Query("", max_length=100)) -> dict[str, Any]:
    return envelope(search_service.suggestions(db, q, settings))
