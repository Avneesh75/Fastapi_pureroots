"""Catalog: listing, filters, sort, search, suggestions, detail, facets, categories and reviews."""
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.catalog import Category, Product, ProductVariant
from tests.factories import (
    auth_headers,
    create_category,
    create_image,
    create_product,
    create_review,
    create_user,
    create_variant,
)

PRODUCTS_URL = "/api/v1/products"


@dataclass
class Catalog:
    atta: Category
    oils: Category
    sweeteners: Category
    wheat_atta: Product
    multigrain: Product
    groundnut_oil: Product
    jaggery: Product
    draft: Product
    hidden: Product
    wheat_1kg: ProductVariant
    wheat_5kg: ProductVariant


@pytest.fixture
def catalog(db: Session) -> Catalog:
    atta = create_category(db, name="Atta & Flours", slug="atta", sort_order=1)
    oils = create_category(db, name="Cold-Pressed Oils", slug="oils", sort_order=2)
    sweeteners = create_category(db, name="Natural Sweeteners", slug="sweeteners", sort_order=3)
    archived = create_category(db, name="Archived", slug="archived", is_active=False)

    wheat_atta = create_product(
        db,
        atta,
        name="Stone-Ground Whole Wheat Atta",
        slug="whole-wheat-atta",
        popularity=90,
        is_bestseller=True,
        tags=["wheat", "atta"],
        key_benefits="Rich in fibre\nStone ground at low temperature",
        nutrition=[{"label": "Protein", "value": "12 g"}],
        short_description="Chakki-fresh atta from Madhya Pradesh.",
    )
    wheat_1kg = create_variant(db, wheat_atta, "1 kg", "199", "240", 1000, stock=50, is_default=True, sort_order=0)
    wheat_5kg = create_variant(db, wheat_atta, "5 kg", "899", "1050", 5000, stock=10, sort_order=1)

    multigrain = create_product(db, atta, name="Multigrain Atta", slug="multigrain-atta", popularity=50, is_featured=True)
    create_variant(db, multigrain, "1 kg", "249", "275", 1000, stock=0)

    groundnut_oil = create_product(
        db,
        oils,
        name="Cold-Pressed Groundnut Oil",
        slug="groundnut-oil",
        popularity=70,
        search_keywords="peanut oil, kachi ghani",
        tax_rate="5.00",
    )
    create_variant(db, groundnut_oil, "1 L", "399", "450", 910, stock=20)
    create_image(db, groundnut_oil, "products/groundnut.jpg")

    jaggery = create_product(
        db, sweeteners, name="Organic Jaggery Powder", slug="jaggery", popularity=10, status="out_of_stock"
    )
    create_variant(db, jaggery, "500 g", "150", "180", 500, stock=5)

    draft = create_product(db, atta, name="Secret Draft Flour", slug="draft-flour", status="draft")
    create_variant(db, draft, "1 kg", "100", "100", 1000)
    hidden = create_product(db, archived, name="Hidden Product", slug="hidden")
    create_variant(db, hidden, "1 kg", "100", "100", 1000)

    reviewer_a = create_user(db, email="rev.a@testmail.in")
    reviewer_b = create_user(db, email="rev.b@testmail.in")
    create_review(db, groundnut_oil, reviewer_a, rating=5)
    create_review(db, groundnut_oil, reviewer_b, rating=4)
    create_review(db, multigrain, reviewer_a, rating=2, is_approved=False)

    return Catalog(
        atta, oils, sweeteners, wheat_atta, multigrain, groundnut_oil, jaggery, draft, hidden, wheat_1kg, wheat_5kg
    )


def slugs(response) -> list[str]:  # type: ignore[no-untyped-def]
    assert response.status_code == 200, response.text
    return [card["slug"] for card in response.json()["data"]]


def test_list_only_listable_products_sorted_by_popularity(client: TestClient, catalog: Catalog) -> None:
    response = client.get(PRODUCTS_URL)
    assert slugs(response) == ["whole-wheat-atta", "groundnut-oil", "multigrain-atta", "jaggery"]
    assert response.json()["meta"] == {"page": 1, "page_size": 24, "total": 4, "total_pages": 1}


def test_product_card_shape(client: TestClient, catalog: Catalog) -> None:
    card = client.get(PRODUCTS_URL).json()["data"][0]
    assert card["slug"] == "whole-wheat-atta"
    assert card["category"] == {"id": str(catalog.atta.id), "name": "Atta & Flours", "slug": "atta"}
    assert card["price_from"] == 199.0
    assert card["mrp_from"] == 240.0
    assert card["max_discount_percent"] == 17
    assert card["default_variant_id"] == str(catalog.wheat_1kg.id)
    assert card["in_stock"] is True
    assert card["is_bestseller"] is True
    assert card["tags"] == ["wheat", "atta"]
    assert card["image_url"] == "/placeholder/product/whole-wheat-atta.svg"
    assert [v["label"] for v in card["variants"]] == ["1 kg", "5 kg"]
    assert card["variants"][1] == {
        "id": str(catalog.wheat_5kg.id),
        "sku": catalog.wheat_5kg.sku,
        "label": "5 kg",
        "weight_grams": 5000,
        "mrp": 1050.0,
        "price": 899.0,
        "discount_percent": 14,
        "in_stock": True,
        "available_quantity": 10,
        # OFFERS_AND_ADMIN §3 (no live offers here)
        "final_price": 899.0,
        "offer_discount": 0.0,
        "offer_discount_percent": 0,
        "offer": None,
    }
    assert card["final_price_from"] == 199.0
    assert card["offer_label"] is None
    assert card["offer_hints"] == []


def test_rating_and_media_image(client: TestClient, catalog: Catalog) -> None:
    card = next(c for c in client.get(PRODUCTS_URL).json()["data"] if c["slug"] == "groundnut-oil")
    assert card["rating_avg"] == 4.5
    assert card["rating_count"] == 2
    assert card["image_url"] == "/media/products/groundnut.jpg"


def test_unapproved_reviews_are_ignored(client: TestClient, catalog: Catalog) -> None:
    card = next(c for c in client.get(PRODUCTS_URL).json()["data"] if c["slug"] == "multigrain-atta")
    assert card["rating_count"] == 0


def test_out_of_stock_status_disables_variants(client: TestClient, catalog: Catalog) -> None:
    card = next(c for c in client.get(PRODUCTS_URL).json()["data"] if c["slug"] == "jaggery")
    assert card["in_stock"] is False
    assert card["variants"][0]["available_quantity"] == 0


def test_filter_by_category(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get(PRODUCTS_URL, params={"category": "atta"})) == ["whole-wheat-atta", "multigrain-atta"]
    assert set(slugs(client.get(PRODUCTS_URL, params={"category": "oils,sweeteners"}))) == {"groundnut-oil", "jaggery"}


def test_filter_in_stock(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get(PRODUCTS_URL, params={"in_stock": "true"})) == ["whole-wheat-atta", "groundnut-oil"]


def test_filter_price_range_matches_any_variant(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get(PRODUCTS_URL, params={"min_price": 300})) == ["whole-wheat-atta", "groundnut-oil"]
    assert slugs(client.get(PRODUCTS_URL, params={"max_price": 250})) == [
        "whole-wheat-atta",
        "multigrain-atta",
        "jaggery",
    ]


def test_filter_weight_rating_featured_bestseller(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get(PRODUCTS_URL, params={"weight": "5 kg"})) == ["whole-wheat-atta"]
    assert slugs(client.get(PRODUCTS_URL, params={"min_rating": 4})) == ["groundnut-oil"]
    assert slugs(client.get(PRODUCTS_URL, params={"featured": "true"})) == ["multigrain-atta"]
    assert slugs(client.get(PRODUCTS_URL, params={"bestseller": "true"})) == ["whole-wheat-atta"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("price_asc", ["jaggery", "whole-wheat-atta", "multigrain-atta", "groundnut-oil"]),
        ("price_desc", ["groundnut-oil", "multigrain-atta", "whole-wheat-atta", "jaggery"]),
        ("rating", ["groundnut-oil"]),
    ],
)
def test_sorting(client: TestClient, catalog: Catalog, sort: str, expected: list[str]) -> None:
    result = slugs(client.get(PRODUCTS_URL, params={"sort": sort}))
    assert result[: len(expected)] == expected


def test_invalid_sort_is_validation_error(client: TestClient, catalog: Catalog) -> None:
    response = client.get(PRODUCTS_URL, params={"sort": "cheapest"})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "sort"


def test_pagination(client: TestClient, catalog: Catalog) -> None:
    response = client.get(PRODUCTS_URL, params={"page": 2, "page_size": 3})
    assert slugs(response) == ["jaggery"]
    assert response.json()["meta"] == {"page": 2, "page_size": 3, "total": 4, "total_pages": 2}


def test_search_matches_name_keywords_tags_and_category(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get("/api/v1/search", params={"q": "groundnut"})) == ["groundnut-oil"]
    assert slugs(client.get("/api/v1/search", params={"q": "PEANUT"})) == ["groundnut-oil"]
    assert slugs(client.get("/api/v1/search", params={"q": "flours"})) == ["whole-wheat-atta", "multigrain-atta"]
    assert slugs(client.get("/api/v1/search", params={"q": "wheat atta"})) == ["whole-wheat-atta"]
    assert slugs(client.get(PRODUCTS_URL, params={"q": "jaggery"})) == ["jaggery"]


def test_search_requires_q(client: TestClient, catalog: Catalog) -> None:
    response = client.get("/api/v1/search")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "q"


def test_search_combines_with_filters(client: TestClient, catalog: Catalog) -> None:
    assert slugs(client.get("/api/v1/search", params={"q": "atta", "in_stock": "true"})) == ["whole-wheat-atta"]


def test_suggestions(client: TestClient, catalog: Catalog) -> None:
    response = client.get("/api/v1/search/suggestions", params={"q": "att"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert [p["slug"] for p in data["products"]] == ["whole-wheat-atta", "multigrain-atta"]
    assert data["products"][0]["price_from"] == 199.0
    assert data["categories"] == [{"name": "Atta & Flours", "slug": "atta"}]
    assert "atta" in data["keywords"]


def test_suggestions_short_query_is_empty(client: TestClient, catalog: Catalog) -> None:
    data = client.get("/api/v1/search/suggestions", params={"q": "a"}).json()["data"]
    assert data == {"products": [], "categories": [], "keywords": []}


def test_product_detail_by_slug_and_id(client: TestClient, catalog: Catalog) -> None:
    by_slug = client.get(f"{PRODUCTS_URL}/whole-wheat-atta")
    assert by_slug.status_code == 200
    detail = by_slug.json()["data"]
    assert detail["key_benefits"] == ["Rich in fibre", "Stone ground at low temperature"]
    assert detail["nutrition"] == [{"label": "Protein", "value": "12 g"}]
    assert detail["images"] == [
        {"url": "/placeholder/product/whole-wheat-atta.svg", "alt": "Stone-Ground Whole Wheat Atta"}
    ]
    assert detail["tax_rate"] == 5.0
    assert detail["seo_title"] == "Stone-Ground Whole Wheat Atta"

    by_id = client.get(f"{PRODUCTS_URL}/{catalog.wheat_atta.id}")
    assert by_id.status_code == 200
    assert by_id.json()["data"]["slug"] == "whole-wheat-atta"


@pytest.mark.parametrize("slug", ["does-not-exist", "draft-flour", "hidden"])
def test_product_detail_not_found(client: TestClient, catalog: Catalog, slug: str) -> None:
    response = client.get(f"{PRODUCTS_URL}/{slug}")
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error": {"code": "NOT_FOUND", "message": "We couldn't find that product.", "details": []},
    }


def test_related_products_prefer_same_category(client: TestClient, catalog: Catalog) -> None:
    related = slugs(client.get(f"{PRODUCTS_URL}/whole-wheat-atta/related"))
    assert related[0] == "multigrain-atta"
    assert "whole-wheat-atta" not in related
    assert set(related) == {"multigrain-atta", "groundnut-oil", "jaggery"}


def test_facets(client: TestClient, catalog: Catalog) -> None:
    response = client.get(f"{PRODUCTS_URL}/filters")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["categories"] == [
        {"slug": "atta", "name": "Atta & Flours", "count": 2},
        {"slug": "oils", "name": "Cold-Pressed Oils", "count": 1},
        {"slug": "sweeteners", "name": "Natural Sweeteners", "count": 1},
    ]
    assert data["price"] == {"min": 150.0, "max": 899.0}
    assert data["weights"] == ["500 g", "1 L", "1 kg", "5 kg"]
    assert data["ratings"] == [4, 3, 2, 1]


def test_facets_scoped_by_category(client: TestClient, catalog: Catalog) -> None:
    data = client.get(f"{PRODUCTS_URL}/filters", params={"category": "atta"}).json()["data"]
    assert data["price"] == {"min": 199.0, "max": 899.0}
    assert data["weights"] == ["1 kg", "5 kg"]


def test_categories_list_and_detail(client: TestClient, catalog: Catalog) -> None:
    response = client.get("/api/v1/categories")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [(c["slug"], c["product_count"]) for c in data] == [("atta", 2), ("oils", 1), ("sweeteners", 1)]
    assert data[0]["image_url"] == "/placeholder/category/atta.svg"

    assert client.get("/api/v1/categories/oils").json()["data"]["name"] == "Cold-Pressed Oils"
    assert client.get("/api/v1/categories/archived").status_code == 404


def test_reviews_list_with_summary(client: TestClient, catalog: Catalog) -> None:
    response = client.get(f"{PRODUCTS_URL}/groundnut-oil/reviews")
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 2
    assert body["meta"]["total"] == 2
    assert body["meta"]["summary"] == {
        "avg": 4.5,
        "count": 2,
        "distribution": {"5": 1, "4": 1, "3": 0, "2": 0, "1": 0},
    }
    assert body["data"][0]["author_name"] == "Asha K."


def test_create_review_once_per_user(client: TestClient, db: Session, catalog: Catalog) -> None:
    user = create_user(db, email="new.reviewer@testmail.in", full_name="Rohan Mehta")
    payload = {"rating": 4, "title": "Nutty and fresh", "body": "Great for tadka."}
    first = client.post(f"{PRODUCTS_URL}/whole-wheat-atta/reviews", json=payload, headers=auth_headers(user))
    assert first.status_code == 201, first.text
    assert first.json()["data"]["author_name"] == "Rohan M."
    assert first.json()["data"]["is_verified_purchase"] is False

    duplicate = client.post(f"{PRODUCTS_URL}/whole-wheat-atta/reviews", json=payload, headers=auth_headers(user))
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CONFLICT"


def test_create_review_requires_auth_and_valid_rating(client: TestClient, db: Session, catalog: Catalog) -> None:
    anonymous = client.post(f"{PRODUCTS_URL}/whole-wheat-atta/reviews", json={"rating": 5})
    assert anonymous.status_code == 401
    user = create_user(db)
    invalid = client.post(f"{PRODUCTS_URL}/whole-wheat-atta/reviews", json={"rating": 6}, headers=auth_headers(user))
    assert invalid.status_code == 422
    assert invalid.json()["error"]["details"][0]["field"] == "rating"
