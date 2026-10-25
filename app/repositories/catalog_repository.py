"""Read-only catalog queries against Django tables.

N+1 avoidance:
* ratings come from one grouped subquery over ``reviews``;
* availability comes from one grouped ``sum(on_hand - reserved)`` over ``stock_levels``;
* variants / images are loaded with ``selectinload``; category via ``contains_eager``.

Offer-aware prices: callers may pass ``final_price(price, product_id, category_id)`` (built by the
catalog service from ``pureroots_pricing.price_unit``). When offers are live and the request sorts
or filters by price, the candidate variants are fetched in ONE query and the final prices are
computed in Python, so sorting / ``min_price`` / ``max_price`` use the post-offer price with no N+1.
"""
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, String, and_, cast, func, or_, select
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.core.constants import LISTABLE_PRODUCT_STATUSES
from app.models.catalog import Category, Coupon, Product, ProductImage, ProductVariant, StockLevel
from app.models.review import Review


@dataclass
class ProductFilters:
    q: str | None = None
    categories: list[str] = field(default_factory=list)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_rating: float | None = None
    in_stock: bool | None = None
    weights: list[str] = field(default_factory=list)
    featured: bool | None = None
    bestseller: bool | None = None
    sort: str = "popularity"


FinalPrice = Callable[[Decimal, uuid.UUID, uuid.UUID], Decimal]
"""``final_price(selling_price, product_id, category_id) -> post-offer unit price``."""


@dataclass
class ProductRow:
    product: Product
    rating_avg: float
    rating_count: int


# --------------------------------------------------------------------------- subqueries


def rating_subquery() -> Any:
    return (
        select(
            Review.product_id.label("product_id"),
            func.avg(Review.rating).label("rating_avg"),
            func.count(Review.id).label("rating_count"),
        )
        .where(Review.is_approved.is_(True))
        .group_by(Review.product_id)
        .subquery("ratings")
    )


def price_subquery() -> Any:
    return (
        select(ProductVariant.product_id.label("product_id"), func.min(ProductVariant.price).label("min_price"))
        .where(ProductVariant.is_active.is_(True))
        .group_by(ProductVariant.product_id)
        .subquery("prices")
    )


def availability_subquery() -> Any:
    return (
        select(
            StockLevel.variant_id.label("variant_id"),
            func.sum(StockLevel.on_hand - StockLevel.reserved).label("available"),
        )
        .group_by(StockLevel.variant_id)
        .subquery("availability")
    )


def in_stock_product_ids() -> Select:
    availability = availability_subquery()
    return (
        select(ProductVariant.product_id)
        .select_from(ProductVariant)
        .join(availability, availability.c.variant_id == ProductVariant.id)
        .where(ProductVariant.is_active.is_(True), availability.c.available > 0)
    )


# --------------------------------------------------------------------------- conditions


def listable_conditions() -> list[ColumnElement[bool]]:
    """Products shown on the storefront: active/out_of_stock in an active category."""
    return [Product.status.in_(LISTABLE_PRODUCT_STATUSES), Category.is_active.is_(True)]


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_condition(q: str) -> ColumnElement[bool] | None:
    """Every word must match name, short description, tags, keywords or category name."""
    terms = [term for term in q.split() if term][:6]
    clauses = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        clauses.append(
            or_(
                Product.name.ilike(pattern, escape="\\"),
                Product.short_description.ilike(pattern, escape="\\"),
                cast(Product.tags, String).ilike(pattern, escape="\\"),
                Product.search_keywords.ilike(pattern, escape="\\"),
                Category.name.ilike(pattern, escape="\\"),
            )
        )
    if not clauses:
        return None
    return and_(*clauses)


def _apply_filters(stmt: Select, filters: ProductFilters, ratings: Any) -> Select:
    stmt = stmt.where(*listable_conditions())
    if filters.q:
        condition = search_condition(filters.q)
        if condition is not None:
            stmt = stmt.where(condition)
    if filters.categories:
        stmt = stmt.where(Category.slug.in_(filters.categories))
    if filters.min_price is not None or filters.max_price is not None:
        variant_ids = select(ProductVariant.product_id).where(ProductVariant.is_active.is_(True))
        if filters.min_price is not None:
            variant_ids = variant_ids.where(ProductVariant.price >= filters.min_price)
        if filters.max_price is not None:
            variant_ids = variant_ids.where(ProductVariant.price <= filters.max_price)
        stmt = stmt.where(Product.id.in_(variant_ids))
    if filters.min_rating:
        stmt = stmt.where(func.coalesce(ratings.c.rating_avg, 0) >= filters.min_rating)
    if filters.in_stock:
        stmt = stmt.where(Product.status == "active", Product.id.in_(in_stock_product_ids()))
    if filters.weights:
        weight_ids = select(ProductVariant.product_id).where(
            ProductVariant.is_active.is_(True), ProductVariant.label.in_(filters.weights)
        )
        stmt = stmt.where(Product.id.in_(weight_ids))
    if filters.featured is not None:
        stmt = stmt.where(Product.is_featured.is_(filters.featured))
    if filters.bestseller is not None:
        stmt = stmt.where(Product.is_bestseller.is_(filters.bestseller))
    return stmt


def _order_by(sort: str, ratings: Any, prices: Any) -> list[Any]:
    if sort == "price_asc":
        order = [func.coalesce(prices.c.min_price, 0).asc()]
    elif sort == "price_desc":
        order = [func.coalesce(prices.c.min_price, 0).desc()]
    elif sort == "newest":
        order = [Product.created_at.desc()]
    elif sort == "rating":
        order = [func.coalesce(ratings.c.rating_avg, 0).desc(), func.coalesce(ratings.c.rating_count, 0).desc()]
    else:
        order = [Product.popularity.desc()]
    return [*order, Product.name.asc(), Product.id.asc()]


def _card_select(ratings: Any) -> Select:
    return (
        select(Product, ratings.c.rating_avg, ratings.c.rating_count)
        .join(Category, Product.category_id == Category.id)
        .outerjoin(ratings, ratings.c.product_id == Product.id)
        .options(
            contains_eager(Product.category),
            selectinload(Product.variants),
            selectinload(Product.images),
        )
    )


def _to_rows(result: Iterable[Any]) -> list[ProductRow]:
    return [
        ProductRow(product=product, rating_avg=float(avg or 0), rating_count=int(count or 0))
        for product, avg, count in result
    ]


# --------------------------------------------------------------------------- product queries


def _list_products_sql(
    db: Session,
    filters: ProductFilters,
    offset: int,
    limit: int,
    extra: Sequence[ColumnElement[bool]] = (),
) -> tuple[list[ProductRow], int]:
    ratings = rating_subquery()
    prices = price_subquery()

    base = (
        select(Product.id)
        .join(Category, Product.category_id == Category.id)
        .outerjoin(ratings, ratings.c.product_id == Product.id)
    )
    base = _apply_filters(base, filters, ratings).where(*extra)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = _card_select(ratings).outerjoin(prices, prices.c.product_id == Product.id)
    stmt = _apply_filters(stmt, filters, ratings).where(*extra)
    stmt = stmt.order_by(*_order_by(filters.sort, ratings, prices)).offset(offset).limit(limit)
    return _to_rows(db.execute(stmt).all()), int(total)


def _in_range(value: Decimal, low: Decimal | None, high: Decimal | None) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)


def _list_products_final_price(
    db: Session, filters: ProductFilters, offset: int, limit: int, final_price: FinalPrice
) -> tuple[list[ProductRow], int]:
    """Price filter / sort on post-offer prices: one candidate query, maths in Python, then one page query."""
    ratings = rating_subquery()
    unpriced = replace(filters, min_price=None, max_price=None)
    stmt = (
        select(Product.id, Product.name, Product.category_id, ProductVariant.price)
        .join(Category, Product.category_id == Category.id)
        .outerjoin(ratings, ratings.c.product_id == Product.id)
        .outerjoin(
            ProductVariant, and_(ProductVariant.product_id == Product.id, ProductVariant.is_active.is_(True))
        )
    )
    stmt = _apply_filters(stmt, unpriced, ratings)

    finals: dict[uuid.UUID, list[Decimal]] = {}
    names: dict[uuid.UUID, str] = {}
    for product_id, name, category_id, price in db.execute(stmt).all():
        values = finals.setdefault(product_id, [])
        names[product_id] = name or ""
        if price is not None:
            values.append(final_price(Decimal(price), product_id, category_id))

    has_price_filter = filters.min_price is not None or filters.max_price is not None
    ids = [
        product_id
        for product_id, values in finals.items()
        if not has_price_filter or any(_in_range(v, filters.min_price, filters.max_price) for v in values)
    ]
    total = len(ids)

    if filters.sort not in ("price_asc", "price_desc"):
        rows, _ = _list_products_sql(db, unpriced, offset, limit, extra=[Product.id.in_(ids)])
        return rows, total

    # Same tie-breakers as ``_order_by``: price, then name, then id (stable sorts).
    ids.sort(key=lambda product_id: (names[product_id], str(product_id)))
    ids.sort(key=lambda product_id: min(finals[product_id], default=Decimal(0)), reverse=filters.sort == "price_desc")
    page_ids = ids[offset : offset + limit]
    by_id = {row.product.id: row for row in select_rows(db, Product.id.in_(page_ids))} if page_ids else {}
    return [by_id[product_id] for product_id in page_ids if product_id in by_id], total


def list_products(
    db: Session,
    filters: ProductFilters,
    offset: int,
    limit: int,
    final_price: FinalPrice | None = None,
) -> tuple[list[ProductRow], int]:
    """Filtered, sorted page of products plus the total count.

    Without ``final_price`` (no live offers) everything runs in SQL on the selling price.
    """
    uses_price = filters.sort in ("price_asc", "price_desc") or (
        filters.min_price is not None or filters.max_price is not None
    )
    if final_price is not None and uses_price:
        return _list_products_final_price(db, filters, offset, limit, final_price)
    return _list_products_sql(db, filters, offset, limit)


def select_rows(
    db: Session,
    *conditions: ColumnElement[bool],
    order_by: Sequence[Any] | None = None,
    limit: int | None = None,
) -> list[ProductRow]:
    """Listable products matching extra conditions, with ratings attached."""
    ratings = rating_subquery()
    stmt = _card_select(ratings).where(*listable_conditions(), *conditions)
    stmt = stmt.order_by(*(order_by or [Product.popularity.desc(), Product.name.asc()]))
    if limit is not None:
        stmt = stmt.limit(limit)
    return _to_rows(db.execute(stmt).all())


def get_product_row(db: Session, slug_or_id: str) -> ProductRow | None:
    try:
        condition = Product.id == uuid.UUID(slug_or_id)
    except ValueError:
        condition = Product.slug == slug_or_id
    rows = select_rows(db, condition, limit=1)
    return rows[0] if rows else None


def get_rows_by_ids(db: Session, product_ids: Sequence[uuid.UUID]) -> list[ProductRow]:
    if not product_ids:
        return []
    return select_rows(db, Product.id.in_(list(product_ids)))


def related_rows(db: Session, product: Product, limit: int = 8) -> list[ProductRow]:
    """Same-category products first, topped up with popular products from other categories."""
    rows = select_rows(db, Product.category_id == product.category_id, Product.id != product.id, limit=limit)
    if len(rows) < limit:
        exclude = [product.id, *(row.product.id for row in rows)]
        rows += select_rows(db, Product.id.not_in(exclude), limit=limit - len(rows))
    return rows


def get_availability(db: Session, variant_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """``{variant_id: max(0, sum(on_hand - reserved))}`` for the given variants."""
    ids = list(set(variant_ids))
    if not ids:
        return {}
    stmt = (
        select(StockLevel.variant_id, func.sum(StockLevel.on_hand - StockLevel.reserved))
        .where(StockLevel.variant_id.in_(ids))
        .group_by(StockLevel.variant_id)
    )
    return {variant_id: max(0, int(total or 0)) for variant_id, total in db.execute(stmt).all()}


def get_variants(db: Session, variant_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, ProductVariant]:
    """Variants with their product (+ category, sibling variants and images) eagerly loaded."""
    ids = list(set(variant_ids))
    if not ids:
        return {}
    stmt = (
        select(ProductVariant)
        .where(ProductVariant.id.in_(ids))
        .options(
            selectinload(ProductVariant.product).selectinload(Product.variants),
            selectinload(ProductVariant.product).selectinload(Product.images),
            selectinload(ProductVariant.product).selectinload(Product.category),
        )
    )
    return {variant.id: variant for variant in db.scalars(stmt)}


def get_variant(db: Session, variant_id: uuid.UUID) -> ProductVariant | None:
    return get_variants(db, [variant_id]).get(variant_id)


def first_image_paths(db: Session, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """First image path (lowest ``sort_order``) per product."""
    ids = list(set(product_ids))
    if not ids:
        return {}
    stmt = (
        select(ProductImage.product_id, ProductImage.image)
        .where(ProductImage.product_id.in_(ids))
        .order_by(ProductImage.product_id, ProductImage.sort_order, ProductImage.created_at)
    )
    paths: dict[uuid.UUID, str] = {}
    for product_id, image in db.execute(stmt).all():
        if image and product_id not in paths:
            paths[product_id] = image
    return paths


# --------------------------------------------------------------------------- categories & facets


def _category_counts_subquery() -> Any:
    return (
        select(Product.category_id.label("category_id"), func.count(Product.id).label("product_count"))
        .where(Product.status.in_(LISTABLE_PRODUCT_STATUSES))
        .group_by(Product.category_id)
        .subquery("category_counts")
    )


def list_categories(db: Session) -> list[tuple[Category, int]]:
    counts = _category_counts_subquery()
    stmt = (
        select(Category, func.coalesce(counts.c.product_count, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .where(Category.is_active.is_(True))
        .order_by(Category.sort_order, Category.name)
    )
    return [(category, int(count)) for category, count in db.execute(stmt).all()]


def get_category(db: Session, slug: str) -> tuple[Category, int] | None:
    counts = _category_counts_subquery()
    stmt = (
        select(Category, func.coalesce(counts.c.product_count, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .where(Category.is_active.is_(True), Category.slug == slug)
    )
    row = db.execute(stmt).first()
    return (row[0], int(row[1])) if row else None


def category_facets(db: Session, q: str | None = None) -> list[tuple[str, str, int]]:
    stmt = (
        select(Category.slug, Category.name, func.count(Product.id))
        .select_from(Category)
        .join(Product, Product.category_id == Category.id)
        .where(*listable_conditions())
        .group_by(Category.id, Category.slug, Category.name, Category.sort_order)
        .order_by(Category.sort_order, Category.name)
    )
    if q:
        condition = search_condition(q)
        if condition is not None:
            stmt = stmt.where(condition)
    return [(slug, name, int(count)) for slug, name, count in db.execute(stmt).all()]


def _variant_scope(stmt: Select, q: str | None, categories: Sequence[str]) -> Select:
    stmt = (
        stmt.select_from(ProductVariant)
        .join(Product, ProductVariant.product_id == Product.id)
        .join(Category, Product.category_id == Category.id)
        .where(ProductVariant.is_active.is_(True), *listable_conditions())
    )
    if categories:
        stmt = stmt.where(Category.slug.in_(list(categories)))
    if q:
        condition = search_condition(q)
        if condition is not None:
            stmt = stmt.where(condition)
    return stmt


def price_range(
    db: Session,
    q: str | None = None,
    categories: Sequence[str] = (),
    final_price: FinalPrice | None = None,
) -> tuple[Decimal, Decimal]:
    """Min/max variant price; post-offer when ``final_price`` is given (one query either way)."""
    if final_price is None:
        stmt = _variant_scope(select(func.min(ProductVariant.price), func.max(ProductVariant.price)), q, categories)
        low, high = db.execute(stmt).one()
        return Decimal(low or 0), Decimal(high or 0)
    stmt = _variant_scope(select(ProductVariant.price, Product.id, Product.category_id), q, categories)
    values = [final_price(Decimal(price), pid, cid) for price, pid, cid in db.execute(stmt).all()]
    return (min(values), max(values)) if values else (Decimal(0), Decimal(0))


def weight_labels(db: Session, q: str | None = None, categories: Sequence[str] = ()) -> list[str]:
    stmt = _variant_scope(select(ProductVariant.label, func.min(ProductVariant.weight_grams)), q, categories)
    stmt = stmt.group_by(ProductVariant.label).order_by(func.min(ProductVariant.weight_grams), ProductVariant.label)
    return [label for label, _ in db.execute(stmt).all()]


def search_categories(db: Session, q: str, limit: int = 4) -> list[Category]:
    pattern = f"%{_escape_like(q)}%"
    stmt = (
        select(Category)
        .where(Category.is_active.is_(True), Category.name.ilike(pattern, escape="\\"))
        .order_by(Category.sort_order, Category.name)
        .limit(limit)
    )
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- coupons


def get_coupon_by_code(db: Session, code: str) -> Coupon | None:
    return db.scalar(select(Coupon).where(func.upper(Coupon.code) == code.strip().upper()))
