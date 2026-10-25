"""/api/v1/products — listing with filters/sort, facets, detail, related and reviews."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import PageParams, page_meta, page_params
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CurrentUser, DbSession, SettingsDep
from app.repositories.catalog_repository import ProductFilters
from app.schemas.catalog import ProductCard, ProductDetail, ProductFiltersOut
from app.schemas.review import ReviewCreate, ReviewOut
from app.services import catalog_service, review_service

router = APIRouter(prefix="/products", tags=["Catalog"])

SortOption = Literal["popularity", "price_asc", "price_desc", "newest", "rating"]


def common_filter_params(
    category: str | None = Query(None, description="Comma-separated category slugs."),
    min_price: float | None = Query(None, ge=0),
    max_price: float | None = Query(None, ge=0),
    min_rating: float | None = Query(None, ge=0, le=5),
    in_stock: bool | None = Query(None, description="Only products that can be bought now."),
    weight: str | None = Query(None, description="Comma-separated variant labels, e.g. `500 g,1 kg`."),
    featured: bool | None = Query(None),
    bestseller: bool | None = Query(None),
    sort: SortOption = Query("popularity"),
) -> ProductFilters:
    """Filters shared by ``/products`` and ``/search`` (everything except ``q``)."""
    return ProductFilters(
        q=None,
        categories=catalog_service.parse_csv(category),
        min_price=catalog_service.parse_money(min_price),
        max_price=catalog_service.parse_money(max_price),
        min_rating=min_rating,
        in_stock=in_stock,
        weights=catalog_service.parse_csv(weight),
        featured=featured,
        bestseller=bestseller,
        sort=sort,
    )


def product_filter_params(
    q: str | None = Query(None, max_length=100, description="Free-text search."),
    filters: ProductFilters = Depends(common_filter_params),
) -> ProductFilters:
    filters.q = (q or "").strip() or None
    return filters


@router.get(
    "",
    response_model=ApiResponse[list[ProductCard]],
    summary="List products",
    description=(
        "Only `active` and `out_of_stock` products in active categories. Filters combine with AND. "
        "`meta` carries pagination (`page`, `page_size` ≤ 60, `total`, `total_pages`)."
    ),
    responses=error_responses(422),
)
def list_products(
    db: DbSession,
    settings: SettingsDep,
    filters: ProductFilters = Depends(product_filter_params),
    page: PageParams = Depends(page_params),
) -> dict[str, Any]:
    cards, total = catalog_service.list_products(db, filters, page.offset, page.page_size, settings)
    return envelope(cards, meta=page_meta(page.page, page.page_size, total))


@router.get(
    "/filters",
    response_model=ApiResponse[ProductFiltersOut],
    summary="Facets for the shop filters",
    description="Category counts, price range, variant weights and rating buckets. Optionally scoped by `q`/`category`.",
)
def product_filters(
    db: DbSession,
    q: str | None = Query(None, max_length=100),
    category: str | None = Query(None, description="Comma-separated category slugs (scopes price & weights)."),
) -> dict[str, Any]:
    return envelope(catalog_service.get_filters(db, (q or "").strip() or None, catalog_service.parse_csv(category)))


@router.get(
    "/{slug_or_id}",
    response_model=ApiResponse[ProductDetail],
    summary="Product detail",
    responses=error_responses(404),
)
def get_product(slug_or_id: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(catalog_service.get_product_detail(db, slug_or_id, settings))


@router.get(
    "/{slug_or_id}/related",
    response_model=ApiResponse[list[ProductCard]],
    summary="Related products (up to 8)",
    responses=error_responses(404),
)
def related_products(slug_or_id: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    return envelope(catalog_service.related_products(db, slug_or_id, settings))


@router.get(
    "/{slug_or_id}/reviews",
    response_model=ApiResponse[list[ReviewOut]],
    summary="Approved reviews",
    description="Paginated; `meta.summary` = `{avg, count, distribution: {5: n, …, 1: n}}`.",
    responses=error_responses(404),
)
def list_reviews(slug_or_id: str, db: DbSession, page: PageParams = Depends(page_params)) -> dict[str, Any]:
    reviews, total, summary = review_service.list_reviews(db, slug_or_id, page.offset, page.page_size)
    meta = {**page_meta(page.page, page.page_size, total), "summary": summary.model_dump()}
    return envelope(reviews, meta=meta)


@router.post(
    "/{slug_or_id}/reviews",
    response_model=ApiResponse[ReviewOut],
    status_code=status.HTTP_201_CREATED,
    summary="Write a review",
    description="One review per customer per product (409 on a duplicate). Flags verified purchases.",
    responses=error_responses(401, 403, 404, 409, 422),
)
def create_review(slug_or_id: str, payload: ReviewCreate, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    review = review_service.create_review(db, user, slug_or_id, payload)
    return envelope(review, message="Thanks for your review!")
