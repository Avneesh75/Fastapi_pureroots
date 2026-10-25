"""Offers (OFFERS_AND_ADMIN §3): catalog display, cart pricing, coupon combinability and order snapshots.

The maths itself is tested in ``backend/shared/pureroots_pricing``; these tests check FastAPI's wiring.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.database import engine
from app.models.catalog import Category, Product, ProductVariant
from app.models.order import Order
from app.models.user import User
from app.repositories import offer_repository
from tests.factories import (
    auth_headers,
    checkout_payload,
    create_category,
    create_coupon,
    create_offer,
    create_product,
    create_user,
    create_variant,
)

PRODUCTS_URL = "/api/v1/products"
CART_URL = "/api/v1/cart"
ORDERS_URL = "/api/v1/orders"


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Shop:
    spices: Category
    atta: Category
    turmeric: Product
    chilli: Product
    wheat: Product
    turmeric_100g: ProductVariant  # 500 / MRP 600
    chilli_100g: ProductVariant  # 99 / MRP 120 (cheap)
    wheat_1kg: ProductVariant  # 199 / MRP 240
    user: User

    @property
    def headers(self) -> dict[str, str]:
        return auth_headers(self.user)


@pytest.fixture
def shop(db: Session) -> Shop:
    spices = create_category(db, name="Spices", slug="spices")
    atta = create_category(db, name="Atta & Flours", slug="atta")
    turmeric = create_product(db, spices, name="Lakadong Turmeric", slug="turmeric", popularity=90)
    turmeric_100g = create_variant(db, turmeric, "100 g", "500", "600", 100, stock=50, is_default=True)
    chilli = create_product(db, spices, name="Kashmiri Chilli", slug="chilli", popularity=80)
    chilli_100g = create_variant(db, chilli, "100 g", "99", "120", 100, stock=50, is_default=True)
    wheat = create_product(db, atta, name="Whole Wheat Atta", slug="wheat", popularity=70)
    wheat_1kg = create_variant(db, wheat, "1 kg", "199", "240", 1000, stock=50, is_default=True)
    user = create_user(db, email="asha@testmail.in")
    return Shop(spices, atta, turmeric, chilli, wheat, turmeric_100g, chilli_100g, wheat_1kg, user)


def cards(client: TestClient, **params) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    response = client.get(PRODUCTS_URL, params=params)
    assert response.status_code == 200, response.text
    return {card["slug"]: card for card in response.json()["data"]}


def add(client: TestClient, shop: Shop, variant: ProductVariant, quantity: int = 1) -> dict:
    response = client.post(
        f"{CART_URL}/items", json={"variant_id": str(variant.id), "quantity": quantity}, headers=shop.headers
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def cart(client: TestClient, shop: Shop) -> dict:
    response = client.get(CART_URL, headers=shop.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def item_for(cart_data: dict, variant: ProductVariant) -> dict:
    return next(item for item in cart_data["items"] if item["variant_id"] == str(variant.id))


# --------------------------------------------------------------------------- catalog


def test_category_offer_on_product_list_and_detail(client: TestClient, db: Session, shop: Shop) -> None:
    ends = now() + timedelta(days=2)
    offer = create_offer(db, scope="category", categories=[shop.spices], value="20", ends_at=ends)

    listed = cards(client)
    turmeric = listed["turmeric"]
    variant = turmeric["variants"][0]
    assert variant["price"] == 500.0  # selling price keeps its meaning
    assert variant["mrp"] == 600.0
    assert variant["final_price"] == 400.0
    assert variant["offer_discount"] == 100.0
    assert variant["offer_discount_percent"] == 20
    assert variant["offer"]["id"] == str(offer.id)
    assert variant["offer"]["name"] == "Spice Festival"
    assert variant["offer"]["label"] == "20% OFF"
    assert abs(datetime.fromisoformat(variant["offer"]["ends_at"].replace("Z", "+00:00")) - ends) < timedelta(seconds=1)
    assert turmeric["price_from"] == 500.0
    assert turmeric["final_price_from"] == 400.0
    assert turmeric["offer_label"] == "20% OFF"
    assert turmeric["offer_hints"] == []

    wheat = listed["wheat"]  # other category: untouched
    assert wheat["variants"][0]["offer"] is None
    assert wheat["variants"][0]["final_price"] == 199.0
    assert wheat["final_price_from"] == 199.0
    assert wheat["offer_label"] is None

    detail = client.get(f"{PRODUCTS_URL}/turmeric").json()["data"]
    assert detail["variants"][0]["final_price"] == 400.0
    assert detail["variants"][0]["offer"]["label"] == "20% OFF"
    assert detail["offer_label"] == "20% OFF"
    assert detail["final_price_from"] == 400.0


def test_expired_scheduled_and_inactive_offers_are_ignored(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Expired", starts_at=now() - timedelta(days=5), ends_at=now() - timedelta(minutes=1))
    create_offer(db, name="Scheduled", starts_at=now() + timedelta(days=1))
    create_offer(db, name="Paused", is_active=False)

    for card in cards(client).values():
        variant = card["variants"][0]
        assert variant["offer"] is None
        assert variant["final_price"] == variant["price"]
        assert card["offer_label"] is None
    assert cart(client, shop)["totals"]["offer_discount"] == 0.0
    add(client, shop, shop.turmeric_100g)
    assert cart(client, shop)["totals"]["offer_discount"] == 0.0


def test_products_scope_and_all_scope(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Chilli week", scope="products", products=[shop.chilli], value="10")
    listed = cards(client)
    assert listed["chilli"]["variants"][0]["final_price"] == 89.1
    assert listed["turmeric"]["variants"][0]["offer"] is None
    assert listed["wheat"]["variants"][0]["offer"] is None

    create_offer(db, name="Sitewide", scope="all", value="5")
    listed = cards(client)
    assert listed["chilli"]["variants"][0]["offer"]["name"] == "Chilli week"  # 10% beats 5%
    assert listed["turmeric"]["variants"][0]["final_price"] == 475.0
    assert listed["wheat"]["variants"][0]["final_price"] == 189.05


def test_flat_offer_skipped_on_cheap_item(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Flat 100", discount_type="flat", value="100")
    listed = cards(client)
    assert listed["chilli"]["variants"][0]["offer"] is None  # 99 <= 100: never price at zero
    assert listed["chilli"]["variants"][0]["final_price"] == 99.0
    assert listed["turmeric"]["variants"][0]["final_price"] == 400.0
    assert listed["turmeric"]["variants"][0]["offer"]["label"] == "₹100 OFF"
    assert listed["wheat"]["variants"][0]["final_price"] == 99.0

    add(client, shop, shop.chilli_100g, 3)
    item = item_for(cart(client, shop), shop.chilli_100g)
    assert item["line_discount"] == 0.0
    assert item["offer"] is None


def test_min_order_offer_applies_only_in_cart(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Big basket", value="10", min_order_amount="1000")

    turmeric = cards(client)["turmeric"]
    assert turmeric["variants"][0]["offer"] is None  # no cart in the catalog
    assert turmeric["variants"][0]["final_price"] == 500.0
    assert turmeric["offer_hints"] == ["Extra 10% OFF on orders above ₹1,000"]

    data = add(client, shop, shop.turmeric_100g, 1)
    assert data["totals"]["items_total"] == 500.0
    assert data["totals"]["offer_discount"] == 0.0

    data = add(client, shop, shop.turmeric_100g, 1)  # items_total reaches 1000
    item = item_for(data, shop.turmeric_100g)
    assert data["totals"]["items_total"] == 1000.0
    assert data["totals"]["offer_discount"] == 100.0
    assert data["totals"]["subtotal"] == 900.0
    assert item["line_discount"] == 100.0
    assert item["unit_price"] == 450.0
    assert item["offer"]["name"] == "Big basket"


def test_max_discount_cap(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Half price", value="50", max_discount_amount="30")
    turmeric = cards(client)["turmeric"]["variants"][0]
    assert turmeric["offer_discount"] == 30.0  # per-unit cap in the catalog
    assert turmeric["final_price"] == 470.0
    assert turmeric["offer_discount_percent"] == 6

    add(client, shop, shop.turmeric_100g, 2)
    data = add(client, shop, shop.wheat_1kg, 1)
    # The cap bounds the offer's total across the cart, spread by line_original (1000 : 199).
    assert data["totals"]["offer_discount"] == 30.0
    discounts = {item["variant_id"]: item["line_discount"] for item in data["items"]}
    assert round(sum(discounts.values()), 2) == 30.0
    assert abs(discounts[str(shop.turmeric_100g.id)] - 25.02) <= 0.01
    assert data["totals"]["subtotal"] == 1169.0


def test_offers_never_stack_best_wins(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, name="Sitewide 10", value="10")  # 50 off turmeric
    create_offer(db, name="Spice flat 60", discount_type="flat", value="60", scope="category", categories=[shop.spices])
    listed = cards(client)
    assert listed["turmeric"]["variants"][0]["offer_discount"] == 60.0  # not 110
    assert listed["turmeric"]["variants"][0]["offer"]["name"] == "Spice flat 60"
    assert listed["wheat"]["variants"][0]["offer"]["name"] == "Sitewide 10"

    # Equal discount (60): higher priority wins.
    create_offer(db, name="Turmeric 12", value="12", scope="products", products=[shop.turmeric], priority=5)
    assert cards(client)["turmeric"]["variants"][0]["offer"]["name"] == "Turmeric 12"

    data = add(client, shop, shop.turmeric_100g, 1)
    assert data["totals"]["offer_discount"] == 60.0
    assert item_for(data, shop.turmeric_100g)["offer"]["name"] == "Turmeric 12"


# --------------------------------------------------------------------------- cart & coupons


def test_cart_totals_math_with_offer_and_coupon(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, scope="category", categories=[shop.spices], value="20")
    create_coupon(db, code="COMBO10", value="10", combinable_with_offers=True)
    add(client, shop, shop.turmeric_100g, 1)
    add(client, shop, shop.wheat_1kg, 2)
    response = client.post(f"{CART_URL}/coupon", json={"code": "combo10"}, headers=shop.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    turmeric = item_for(data, shop.turmeric_100g)
    assert turmeric["unit_mrp"] == 600.0
    assert turmeric["unit_original_price"] == 500.0
    assert turmeric["unit_price"] == 400.0
    assert turmeric["line_original"] == 500.0
    assert turmeric["line_discount"] == 100.0
    assert turmeric["line_total"] == 400.0
    assert turmeric["offer"]["label"] == "20% OFF"
    assert turmeric["variants"][0]["final_price"] == 400.0
    wheat = item_for(data, shop.wheat_1kg)
    assert wheat["unit_price"] == wheat["unit_original_price"] == 199.0
    assert wheat["line_total"] == wheat["line_original"] == 398.0
    assert wheat["offer"] is None

    totals = data["totals"]
    assert totals["mrp_total"] == 1080.0
    assert totals["items_total"] == 898.0
    assert totals["offer_discount"] == 100.0
    assert totals["subtotal"] == 798.0  # post-offer
    assert totals["savings"] == 182.0  # mrp_total - items_total
    assert totals["coupon_discount"] == 79.8  # 10% of the post-offer subtotal
    assert totals["shipping"] == 79.0  # 798 - 79.80 < 999
    assert totals["amount_to_free_shipping"] == 280.8
    assert totals["tax_included"] == 34.2
    assert totals["total"] == 797.2  # 798 - 79.80 + 79


def test_free_shipping_threshold_uses_post_offer_subtotal(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, scope="category", categories=[shop.spices], value="20")
    data = add(client, shop, shop.turmeric_100g, 2)  # items_total 1000, subtotal 800
    assert data["totals"]["subtotal"] == 800.0
    assert data["totals"]["shipping"] == 79.0
    assert data["totals"]["total"] == 879.0


def test_non_combinable_coupon_rejected_with_409(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, scope="category", categories=[shop.spices], value="20")
    create_coupon(db, code="SAVE10", value="10")  # combinable_with_offers defaults to False
    add(client, shop, shop.turmeric_100g, 1)

    response = client.post(f"{CART_URL}/coupon", json={"code": "SAVE10"}, headers=shop.headers)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "COUPON_NOT_COMBINABLE"
    assert error["message"] == "This coupon can't be combined with ongoing offers."
    assert cart(client, shop)["coupon_code"] is None


def test_non_combinable_coupon_fine_without_offer_discount(client: TestClient, db: Session, shop: Shop) -> None:
    create_offer(db, scope="category", categories=[shop.spices], value="20")
    create_coupon(db, code="SAVE10", value="10")
    add(client, shop, shop.wheat_1kg, 2)  # atta: no offer applies

    response = client.post(f"{CART_URL}/coupon", json={"code": "SAVE10"}, headers=shop.headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["totals"]["coupon_discount"] == 39.8


def test_reprice_drops_non_combinable_coupon(client: TestClient, db: Session, shop: Shop) -> None:
    create_coupon(db, code="SAVE10", value="10")
    add(client, shop, shop.turmeric_100g, 1)
    assert client.post(f"{CART_URL}/coupon", json={"code": "SAVE10"}, headers=shop.headers).status_code == 200

    create_offer(db, scope="category", categories=[shop.spices], value="20")  # offer starts afterwards
    data = cart(client, shop)
    assert data["coupon_code"] is None
    assert data["totals"]["coupon_discount"] == 0.0
    assert data["totals"]["offer_discount"] == 100.0
    assert any("SAVE10" in message and "combined with ongoing offers" in message for message in data["messages"])


def test_checkout_rejects_coupon_made_non_combinable_by_new_offer(
    client: TestClient, db: Session, shop: Shop
) -> None:
    create_coupon(db, code="SAVE10", value="10")
    add(client, shop, shop.turmeric_100g, 1)
    assert client.post(f"{CART_URL}/coupon", json={"code": "SAVE10"}, headers=shop.headers).status_code == 200
    create_offer(db, scope="category", categories=[shop.spices], value="20")

    response = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COUPON_NOT_COMBINABLE"
    # The re-price dropped the coupon, so a retry goes through at the offer price.
    retry = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers)
    assert retry.status_code == 201, retry.text
    assert retry.json()["data"]["coupon_code"] is None
    assert retry.json()["data"]["subtotal"] == 400.0


# --------------------------------------------------------------------------- orders


def test_order_snapshots_offer_fields(client: TestClient, db: Session, shop: Shop) -> None:
    offer = create_offer(db, scope="category", categories=[shop.spices], value="20")
    add(client, shop, shop.turmeric_100g, 2)
    add(client, shop, shop.wheat_1kg, 1)

    response = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers)
    assert response.status_code == 201, response.text
    order = response.json()["data"]
    assert order["items_total"] == 1199.0
    assert order["offer_discount_total"] == 200.0
    assert order["subtotal"] == 999.0  # post-offer, like the cart
    assert order["discount_total"] == 0.0
    assert order["shipping_fee"] == 0.0  # 999 >= threshold
    assert order["grand_total"] == 999.0

    items = {item["sku"]: item for item in order["items"]}
    turmeric = items[shop.turmeric_100g.sku]
    assert turmeric["unit_mrp"] == 600.0
    assert turmeric["unit_original_price"] == 500.0
    assert turmeric["unit_price"] == 400.0
    assert turmeric["line_total"] == 800.0
    assert turmeric["offer_discount"] == 200.0
    assert turmeric["offer_id"] == str(offer.id)
    assert turmeric["offer_name"] == "Spice Festival"
    assert turmeric["offer_label"] == "20% OFF"
    wheat = items[shop.wheat_1kg.sku]
    assert wheat["unit_original_price"] == wheat["unit_price"] == 199.0
    assert wheat["offer_discount"] == 0.0
    assert wheat["offer_id"] is None and wheat["offer_name"] is None and wheat["offer_label"] is None

    # Snapshot survives the offer ending.
    offer.is_active = False
    db.commit()
    detail = client.get(f"{ORDERS_URL}/{order['order_number']}", headers=shop.headers).json()["data"]
    assert detail["offer_discount_total"] == 200.0
    assert {i["sku"]: i["offer_label"] for i in detail["items"]}[shop.turmeric_100g.sku] == "20% OFF"

    summary = client.get(ORDERS_URL, headers=shop.headers).json()["data"][0]
    assert summary["offer_discount_total"] == 200.0

    row = db.query(Order).filter_by(order_number=order["order_number"]).one()
    db.refresh(row)
    assert str(row.offer_discount_total) == "200.00"


# --------------------------------------------------------------------------- price sort / filter


def test_price_sort_and_filters_use_final_price(client: TestClient, db: Session, shop: Shop) -> None:
    def order(**params) -> list[str]:  # type: ignore[no-untyped-def]
        response = client.get(PRODUCTS_URL, params=params)
        assert response.status_code == 200, response.text
        return [card["slug"] for card in response.json()["data"]]

    assert order(sort="price_asc") == ["chilli", "wheat", "turmeric"]
    create_offer(db, scope="products", products=[shop.turmeric], value="70")  # turmeric 500 -> 150
    assert order(sort="price_asc") == ["chilli", "turmeric", "wheat"]
    assert order(sort="price_desc") == ["wheat", "turmeric", "chilli"]
    assert order(max_price=160) == ["turmeric", "chilli"]  # popularity order
    assert order(min_price=160) == ["wheat"]
    assert order(min_price=100, sort="price_asc", page_size=1) == ["turmeric"]
    meta = client.get(PRODUCTS_URL, params={"min_price": 100, "page_size": 1}).json()["meta"]
    assert meta["total"] == 2

    facets = client.get(f"{PRODUCTS_URL}/filters").json()["data"]
    assert facets["price"] == {"min": 99.0, "max": 199.0}


# --------------------------------------------------------------------------- repository


def test_live_rules_loads_m2m_ids_in_three_queries(db: Session, shop: Shop) -> None:
    by_category = create_offer(db, name="Cat", scope="category", categories=[shop.spices, shop.atta])
    by_product = create_offer(db, name="Prod", scope="products", products=[shop.chilli])
    create_offer(db, name="All", scope="all")
    create_offer(db, name="Old", ends_at=now() - timedelta(days=1))

    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        rules = {rule.name: rule for rule in offer_repository.live_rules(db, now())}
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(statements) == 3
    assert set(rules) == {"Cat", "Prod", "All"}
    assert rules["Cat"].id == str(by_category.id)
    assert rules["Cat"].category_ids == frozenset({str(shop.spices.id), str(shop.atta.id)})
    assert rules["Prod"].id == str(by_product.id)
    assert rules["Prod"].product_ids == frozenset({str(shop.chilli.id)})
    assert rules["All"].category_ids == frozenset() and rules["All"].product_ids == frozenset()
    assert rules["All"].starts_at is None or rules["All"].starts_at.tzinfo is not None
