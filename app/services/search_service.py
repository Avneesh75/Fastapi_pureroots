"""Search-as-you-type suggestions."""
import re

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.catalog import Product
from app.repositories import catalog_repository
from app.schemas.catalog import SuggestedCategory, SuggestedProduct, SuggestionsOut
from app.services.catalog_service import active_variants, product_image_url
from app.services.pricing_service import money

MAX_PRODUCTS = 6
MAX_CATEGORIES = 4
MAX_KEYWORDS = 6
MIN_QUERY_LENGTH = 2


def _keywords_from(products: list[Product], q: str) -> list[str]:
    needle = q.lower()
    seen: set[str] = set()
    keywords: list[str] = []
    for product in products:
        candidates = [str(tag) for tag in (product.tags or []) if isinstance(tag, (str, int))]
        candidates += re.split(r"[,\n]", product.search_keywords or "")
        for candidate in candidates:
            keyword = " ".join(candidate.split()).lower()
            if keyword and needle in keyword and keyword not in seen:
                seen.add(keyword)
                keywords.append(keyword)
                if len(keywords) >= MAX_KEYWORDS:
                    return keywords
    return keywords


def suggestions(db: Session, q: str, settings: Settings) -> SuggestionsOut:
    query = " ".join(q.split())
    if len(query) < MIN_QUERY_LENGTH:
        return SuggestionsOut(products=[], categories=[], keywords=[])

    condition = catalog_repository.search_condition(query)
    rows = catalog_repository.select_rows(db, condition, limit=MAX_PRODUCTS) if condition is not None else []
    products = [row.product for row in rows]

    suggested_products = []
    for product in products:
        variants = active_variants(product)
        price_from = min((variant.price for variant in variants), default=0)
        suggested_products.append(
            SuggestedProduct(
                name=product.name,
                slug=product.slug,
                image_url=product_image_url(product, settings),
                price_from=money(price_from),
            )
        )

    categories = catalog_repository.search_categories(db, query, MAX_CATEGORIES)
    return SuggestionsOut(
        products=suggested_products,
        categories=[SuggestedCategory(name=category.name, slug=category.slug) for category in categories],
        keywords=_keywords_from(products, query),
    )
