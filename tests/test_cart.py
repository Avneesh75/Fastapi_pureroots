"""Cart: guest cart via pr_cart cookie, add/update/remove, stock clamping, coupons, merge on login."""
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.cart import Cart, CartItem
from app.models.catalog import ProductVariant
from tests.factories import (
    DEFAULT_PASSWORD,
    auth_headers,
    create_category,
    create_coupon,
    create_product,
    create_user,
    create_variant,
    set_stock,
)

CART_URL = "/api/v1/cart"


@dataclass
class Stock:
    small: ProductVariant  # Almonds 500 g, 5 in stock
    large: ProductVariant  # Almonds 1 kg, 3 in stock
    sold_out: ProductVariant
    other: ProductVariant  # a different product


@pytest.fixture
def stock(db: Session) -> Stock:
    nuts = create_category(db, name="Dry Fruits", slug="dry-fruits")
    almonds = create_product(db, nuts, name="Organic Almonds", slug="almonds", tax_rate="5.00")
    small = create_variant(db, almonds, "500 g", "199", "240", 500, stock=5, is_default=True, sort_order=0)
    large = create_variant(db, almonds, "1 kg", "379", "450", 1000, stock=3, sort_order=1)
    cashews = create_product(db, nuts, name="Cashews", slug="cashews")
    sold_out = create_variant(db, cashews, "250 g", "299", "320", 250, stock=0)
    dates = create_product(db, nuts, name="Medjool Dates", slug="dates")
    other = create_variant(db, dates, "500 g", "650", "700", 500, stock=40)
    return Stock(small, large, sold_out, other)


def add(client: TestClient, variant: ProductVariant, quantity: int = 1, **kwargs):  # type: ignore[no-untyped-def]
    return client.post(f"{CART_URL}/items", json={"variant_id": str(variant.id), "quantity": quantity}, **kwargs)


def test_new_guest_gets_empty_cart_and_cookie(client: TestClient, stock: Stock) -> None:
    response = client.get(CART_URL)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] is None
    assert data["items"] == []
    assert data["item_count"] == 0
    assert data["totals"]["total"] == 0.0
    assert data["totals"]["currency"] == "INR"
    assert client.cookies.get("pr_cart")


def test_guest_add_item_prices_cart(client: TestClient, stock: Stock) -> None:
    response = add(client, stock.small, 2)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["item_count"] == 2
    item = data["items"][0]
    assert item["product_name"] == "Organic Almonds"
    assert item["variant_label"] == "500 g"
    assert item["unit_price"] == 199.0
    assert item["line_total"] == 398.0
    assert item["available_quantity"] == 5
    assert [v["label"] for v in item["variants"]] == ["500 g", "1 kg"]
    totals = data["totals"]
    assert totals["mrp_total"] == 480.0
    assert totals["subtotal"] == 398.0
    assert totals["savings"] == 82.0
    assert totals["shipping"] == 79.0
    assert totals["total"] == 477.0
    assert totals["tax_included"] == 18.95
    assert totals["free_shipping_threshold"] == 999.0
    assert totals["amount_to_free_shipping"] == 601.0

    # The same guest (cookie) sees the same cart.
    again = client.get(CART_URL).json()["data"]
    assert again["id"] == data["id"]


def test_adding_same_variant_accumulates(client: TestClient, stock: Stock) -> None:
    add(client, stock.small, 1)
    data = add(client, stock.small, 2).json()["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["quantity"] == 3


def test_update_and_remove_item(client: TestClient, stock: Stock) -> None:
    item_id = add(client, stock.small, 1).json()["data"]["items"][0]["id"]
    updated = client.patch(f"{CART_URL}/items/{item_id}", json={"quantity": 3})
    assert updated.status_code == 200
    assert updated.json()["data"]["items"][0]["quantity"] == 3

    removed = client.delete(f"{CART_URL}/items/{item_id}")
    assert removed.status_code == 200
    assert removed.json()["data"]["items"] == []


def test_quantity_zero_removes_item(client: TestClient, stock: Stock) -> None:
    item_id = add(client, stock.small, 1).json()["data"]["items"][0]["id"]
    response = client.patch(f"{CART_URL}/items/{item_id}", json={"quantity": 0})
    assert response.json()["data"]["items"] == []


def test_add_clamps_to_available_stock_with_message(client: TestClient, stock: Stock) -> None:
    data = add(client, stock.small, 10).json()["data"]
    assert data["items"][0]["quantity"] == 5
    assert data["messages"] == ["Organic Almonds 500 g: only 5 left, quantity adjusted."]


def test_add_sold_out_variant_is_409(client: TestClient, stock: Stock) -> None:
    response = add(client, stock.sold_out, 1)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"][0]["sku"] == stock.sold_out.sku


def test_add_unknown_variant_is_404(client: TestClient, stock: Stock) -> None:
    response = client.post(
        f"{CART_URL}/items", json={"variant_id": "3f0d4c1e-8f59-4a61-9a1d-2f1b7e6e9d10", "quantity": 1}
    )
    assert response.status_code == 404


def test_cart_read_clamps_when_stock_drops(client: TestClient, db: Session, stock: Stock) -> None:
    add(client, stock.small, 4)
    set_stock(db, stock.small, on_hand=2)
    data = client.get(CART_URL).json()["data"]
    assert data["items"][0]["quantity"] == 2
    assert "Organic Almonds 500 g: only 2 left, quantity adjusted." in data["messages"]


def test_out_of_stock_line_excluded_from_totals(client: TestClient, db: Session, stock: Stock) -> None:
    add(client, stock.small, 1)
    add(client, stock.other, 1)
    set_stock(db, stock.small, on_hand=0)
    data = client.get(CART_URL).json()["data"]
    small_line = next(item for item in data["items"] if item["variant_id"] == str(stock.small.id))
    assert small_line["in_stock"] is False
    assert data["totals"]["subtotal"] == 650.0
    assert any("out of stock" in message for message in data["messages"])


def test_change_variant_within_product(client: TestClient, stock: Stock) -> None:
    item_id = add(client, stock.small, 2).json()["data"]["items"][0]["id"]
    response = client.patch(f"{CART_URL}/items/{item_id}", json={"variant_id": str(stock.large.id)})
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item["variant_label"] == "1 kg"
    assert item["quantity"] == 2


def test_change_variant_to_other_product_rejected(client: TestClient, stock: Stock) -> None:
    item_id = add(client, stock.small, 1).json()["data"]["items"][0]["id"]
    response = client.patch(f"{CART_URL}/items/{item_id}", json={"variant_id": str(stock.other.id)})
    assert response.status_code == 422


def test_other_guest_cannot_touch_item(client: TestClient, test_app, stock: Stock) -> None:  # type: ignore[no-untyped-def]
    item_id = add(client, stock.small, 1).json()["data"]["items"][0]["id"]
    with TestClient(test_app, headers={"X-Requested-With": "PureRoot"}) as stranger:
        response = stranger.delete(f"{CART_URL}/items/{item_id}")
    assert response.status_code == 404


def test_apply_and_remove_percent_coupon(client: TestClient, db: Session, stock: Stock) -> None:
    create_coupon(db, code="SAVE10", discount_type="percent", value="10", max_discount="50")
    add(client, stock.small, 2)
    applied = client.post(f"{CART_URL}/coupon", json={"code": "save10"})
    assert applied.status_code == 200, applied.text
    data = applied.json()["data"]
    assert data["coupon_code"] == "SAVE10"
    assert data["totals"]["coupon_discount"] == 39.8
    assert data["totals"]["total"] == 437.2  # 398 - 39.80 + 79
    assert "Coupon SAVE10 applied." in data["messages"]

    removed = client.delete(f"{CART_URL}/coupon").json()["data"]
    assert removed["coupon_code"] is None
    assert removed["totals"]["coupon_discount"] == 0.0


def test_invalid_coupon_is_validation_error(client: TestClient, stock: Stock) -> None:
    add(client, stock.small, 1)
    response = client.post(f"{CART_URL}/coupon", json={"code": "NOPE"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "This coupon code is not valid."
    assert error["details"][0]["field"] == "code"


def test_coupon_min_order_value(client: TestClient, db: Session, stock: Stock) -> None:
    create_coupon(db, code="BIG100", discount_type="flat", value="100", min_order_value="1000")
    add(client, stock.small, 1)
    response = client.post(f"{CART_URL}/coupon", json={"code": "BIG100"})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Add ₹801 more to use BIG100."


def test_inactive_coupon_rejected(client: TestClient, db: Session, stock: Stock) -> None:
    create_coupon(db, code="OLD", is_active=False)
    add(client, stock.small, 1)
    assert client.post(f"{CART_URL}/coupon", json={"code": "OLD"}).status_code == 422


def test_express_delivery_pricing(client: TestClient, stock: Stock) -> None:
    add(client, stock.other, 2)  # 1300 → free standard shipping
    assert client.get(CART_URL).json()["data"]["totals"]["shipping"] == 0.0
    express = client.get(CART_URL, params={"delivery_method": "express"}).json()["data"]["totals"]
    assert express["shipping"] == 149.0
    assert express["total"] == 1449.0


def test_user_cart_via_bearer(client: TestClient, db: Session, stock: Stock) -> None:
    user = create_user(db)
    response = add(client, stock.small, 1, headers=auth_headers(user))
    assert response.status_code == 200
    cart = db.query(Cart).filter(Cart.user_id == user.id).one()
    assert cart.session_key is None


def test_guest_cart_merges_into_user_cart_on_login(client: TestClient, db: Session, stock: Stock) -> None:
    user = create_user(db, email="merge@testmail.in")
    existing = Cart(user_id=user.id)
    existing.items.append(CartItem(variant_id=stock.small.id, quantity=1))
    db.add(existing)
    db.commit()

    add(client, stock.small, 2)
    add(client, stock.other, 1)
    assert client.cookies.get("pr_cart")

    login = client.post("/api/v1/auth/login", json={"email": "merge@testmail.in", "password": DEFAULT_PASSWORD})
    assert login.status_code == 200
    assert client.cookies.get("pr_cart") is None

    data = client.get(CART_URL).json()["data"]  # authenticated by the pr_access cookie
    quantities = {item["variant_id"]: item["quantity"] for item in data["items"]}
    assert quantities == {str(stock.small.id): 3, str(stock.other.id): 1}
    db.expire_all()
    assert db.query(Cart).count() == 1
