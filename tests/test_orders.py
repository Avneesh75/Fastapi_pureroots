"""Orders: checkout, inventory side effects, state machine, cancellation, lookup and ownership."""
import re
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import InsufficientStockError, ServiceUnavailableError
from app.models.cart import CartItem
from app.models.order import CouponRedemption, Order, OrderStatusHistory, Payment
from app.models.user import Address, User
from tests.conftest import INTERNAL_HEADERS
from tests.factories import (
    auth_headers,
    checkout_payload,
    create_category,
    create_coupon,
    create_product,
    create_user,
    create_variant,
)
from tests.fakes import FakeInventory

ORDERS_URL = "/api/v1/orders"


@dataclass
class Shop:
    user: User
    headers: dict[str, str]
    variant_id: str
    sku: str


@pytest.fixture
def shop(db: Session) -> Shop:
    category = create_category(db, name="Ghee & Oils", slug="ghee")
    ghee = create_product(db, category, name="A2 Desi Cow Ghee", slug="a2-ghee", tax_rate="12.00")
    variant = create_variant(db, ghee, "500 ml", "799", "899", 500, stock=30, is_default=True)
    user = create_user(db, email="asha@testmail.in")
    return Shop(user=user, headers=auth_headers(user), variant_id=str(variant.id), sku=variant.sku)


def fill_cart(client: TestClient, shop: Shop, quantity: int = 1, headers: dict[str, str] | None = None) -> None:
    response = client.post(
        "/api/v1/cart/items",
        json={"variant_id": shop.variant_id, "quantity": quantity},
        headers=shop.headers if headers is None else headers,
    )
    assert response.status_code == 200, response.text


def place(client: TestClient, shop: Shop, **overrides):  # type: ignore[no-untyped-def]
    return client.post(ORDERS_URL, json=checkout_payload(**overrides), headers=shop.headers)


def set_status(client: TestClient, order_id: str, status: str):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/internal/orders/{order_id}/status",
        json={"status": status, "note": "", "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )


def test_place_order_happy_path(client: TestClient, db: Session, shop: Shop, inventory: FakeInventory) -> None:
    fill_cart(client, shop, 2)
    response = place(client, shop)
    assert response.status_code == 201, response.text
    order = response.json()["data"]

    assert re.fullmatch(r"PR-\d{6}-[A-Z0-9]{5}", order["order_number"])
    assert order["status"] == "confirmed"
    assert order["payment_status"] == "cod_pending"
    assert order["payment_method"] == "cod"
    assert order["subtotal"] == 1598.0
    assert order["shipping_fee"] == 0.0
    assert order["grand_total"] == 1598.0
    assert order["tax_total"] == 171.21  # 1598 - 1598/1.12
    assert order["item_count"] == 2
    assert order["items"][0]["sku"] == shop.sku
    assert order["items"][0]["line_total"] == 1598.0
    assert [h["to_status"] for h in order["status_history"]] == ["pending", "confirmed"]
    assert order["can_cancel"] is True
    timeline = {step["status"]: step["reached"] for step in order["timeline"]}
    assert timeline["confirmed"] is True and timeline["shipped"] is False
    assert order["shipping_address"]["state"] == "Karnataka"

    assert inventory.actions == ["reserve"]
    reserved = inventory.reserved_items[order["id"]]
    assert reserved == [{"variant_id": shop.variant_id, "quantity": 2}]

    db.expire_all()
    stored = db.query(Order).one()
    assert stored.user_id == shop.user.id
    assert db.query(Payment).filter(Payment.order_id == stored.id).one().status == "pending"
    assert db.query(CartItem).count() == 0

    cart = client.get("/api/v1/cart", headers=shop.headers).json()["data"]
    assert cart["items"] == []


def test_checkout_saves_address_when_requested(client: TestClient, db: Session, shop: Shop) -> None:
    fill_cart(client, shop)
    assert place(client, shop, save_address=True).status_code == 201
    db.expire_all()
    address = db.query(Address).filter(Address.user_id == shop.user.id).one()
    assert address.is_default is True
    assert address.pincode == "560038"


def test_checkout_with_saved_address_id(client: TestClient, db: Session, shop: Shop) -> None:
    created = client.post(
        "/api/v1/addresses",
        json={
            "label": "Home",
            "full_name": "Asha Kulkarni",
            "phone": "9876543210",
            "line1": "221B Residency Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560025",
        },
        headers=shop.headers,
    )
    assert created.status_code == 201
    fill_cart(client, shop)
    response = place(client, shop, shipping_address=None, address_id=created.json()["data"]["id"])
    assert response.status_code == 201, response.text
    assert response.json()["data"]["shipping_address"]["line1"] == "221B Residency Road"


def test_checkout_with_coupon_records_redemption(client: TestClient, db: Session, shop: Shop) -> None:
    create_coupon(db, code="GHEE100", discount_type="flat", value="100")
    fill_cart(client, shop, 1)
    client.post("/api/v1/cart/coupon", json={"code": "GHEE100"}, headers=shop.headers)
    response = place(client, shop)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["discount_total"] == 100.0
    assert data["coupon_code"] == "GHEE100"
    assert data["shipping_fee"] == 79.0  # 799 - 100 = 699 < 999
    assert data["grand_total"] == 778.0
    db.expire_all()
    assert db.query(CouponRedemption).one().discount_amount == 100


def test_guest_checkout_and_lookup(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop, 1, headers={})
    response = client.post(ORDERS_URL, json=checkout_payload(customer={
        "full_name": "Guest Buyer", "email": "Guest@TestMail.in", "phone": "9123456789"
    }))
    assert response.status_code == 201, response.text
    order_number = response.json()["data"]["order_number"]
    assert response.json()["data"]["shipping_fee"] == 79.0

    found = client.post(f"{ORDERS_URL}/lookup", json={"order_number": order_number, "email": "guest@testmail.in"})
    assert found.status_code == 200
    assert found.json()["data"]["customer_name"] == "Guest Buyer"

    wrong = client.post(f"{ORDERS_URL}/lookup", json={"order_number": order_number, "email": "other@testmail.in"})
    assert wrong.status_code == 404


def test_insufficient_stock_from_inventory_creates_no_order(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory
) -> None:
    fill_cart(client, shop, 2)
    details = [{"variant_id": shop.variant_id, "sku": shop.sku, "requested": 2, "available": 1}]
    inventory.reserve_error = InsufficientStockError("Some items are no longer available.", details=details)

    response = place(client, shop)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"] == details

    db.expire_all()
    assert db.query(Order).count() == 0
    assert db.query(OrderStatusHistory).count() == 0
    assert db.query(CartItem).count() == 1  # cart untouched


def test_inventory_unreachable_returns_503(client: TestClient, db: Session, shop: Shop, inventory: FakeInventory) -> None:
    fill_cart(client, shop)
    inventory.reserve_error = ServiceUnavailableError("We couldn't reach our inventory system.")
    response = place(client, shop)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    db.expire_all()
    assert db.query(Order).count() == 0


def test_empty_cart_cannot_checkout(client: TestClient, shop: Shop, inventory: FakeInventory) -> None:
    response = place(client, shop)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Your cart is empty."
    assert inventory.calls == []


def test_checkout_validation_errors(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    bad = checkout_payload()
    bad["customer"]["phone"] = "12345"
    bad["shipping_address"]["pincode"] = "012345"
    bad["shipping_address"]["state"] = "Narnia"
    response = client.post(ORDERS_URL, json=bad, headers=shop.headers)
    assert response.status_code == 422
    fields = {detail["field"] for detail in response.json()["error"]["details"]}
    assert fields == {"customer.phone", "shipping_address.pincode", "shipping_address.state"}


def test_unknown_payment_method_rejected(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    response = place(client, shop, payment_method="online")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "payment_method"


def test_customer_cancel_releases_stock(client: TestClient, shop: Shop, inventory: FakeInventory) -> None:
    fill_cart(client, shop)
    order_number = place(client, shop).json()["data"]["order_number"]
    response = client.post(f"{ORDERS_URL}/{order_number}/cancel", headers=shop.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "cancelled"
    assert data["can_cancel"] is False
    assert data["status_history"][-1]["changed_by"] == "customer"
    assert inventory.actions == ["reserve", "release"]


def test_internal_status_to_shipped_commits_stock(client: TestClient, shop: Shop, inventory: FakeInventory) -> None:
    fill_cart(client, shop)
    order = place(client, shop).json()["data"]
    for status in ("processing", "packed"):
        assert set_status(client, order["id"], status).status_code == 200
    assert "commit" not in inventory.actions

    shipped = set_status(client, order["id"], "shipped")
    assert shipped.status_code == 200, shipped.text
    data = shipped.json()["data"]
    assert data["status"] == "shipped"
    assert data["status_history"][-1] == {
        **data["status_history"][-1],
        "from_status": "packed",
        "to_status": "shipped",
        "changed_by": "admin:priya",
    }
    assert inventory.actions == ["reserve", "commit"]

    cancel = client.post(f"{ORDERS_URL}/{order['order_number']}/cancel", headers=shop.headers)
    assert cancel.status_code == 409


def test_delivered_cod_is_paid_and_refund_returns_stock(
    client: TestClient, shop: Shop, inventory: FakeInventory
) -> None:
    fill_cart(client, shop)
    order = place(client, shop).json()["data"]
    for status in ("processing", "packed", "shipped", "delivered"):
        assert set_status(client, order["id"], status).status_code == 200
    detail = client.get(f"{ORDERS_URL}/{order['order_number']}", headers=shop.headers).json()["data"]
    assert detail["payment_status"] == "paid"
    assert all(step["reached"] for step in detail["timeline"])

    refunded = set_status(client, order["id"], "refunded")
    assert refunded.status_code == 200
    assert refunded.json()["data"]["payment_status"] == "refunded"
    assert inventory.actions == ["reserve", "commit", "return"]


def test_invalid_transition_is_conflict(client: TestClient, shop: Shop, inventory: FakeInventory) -> None:
    fill_cart(client, shop)
    order = place(client, shop).json()["data"]
    response = set_status(client, order["id"], "delivered")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["details"][0]["allowed"] == ["cancelled", "processing"]
    assert inventory.actions == ["reserve"]


def test_cancelled_unpaid_order_cannot_be_refunded(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    order = place(client, shop).json()["data"]
    assert set_status(client, order["id"], "cancelled").status_code == 200
    assert set_status(client, order["id"], "refunded").status_code == 409


def test_unknown_status_value_is_validation_error(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    order = place(client, shop).json()["data"]
    assert set_status(client, order["id"], "teleported").status_code == 422


def test_order_list_and_detail_for_owner(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    order_number = place(client, shop).json()["data"]["order_number"]
    listing = client.get(ORDERS_URL, headers=shop.headers)
    assert listing.status_code == 200
    summaries = listing.json()["data"]
    assert [s["order_number"] for s in summaries] == [order_number]
    assert summaries[0]["item_count"] == 1
    assert summaries[0]["first_item_image"] == "/placeholder/product/a2-ghee.svg"
    assert listing.json()["meta"]["total"] == 1
    assert client.get(f"{ORDERS_URL}/{order_number}", headers=shop.headers).status_code == 200


def test_other_customer_cannot_see_or_cancel_order(client: TestClient, db: Session, shop: Shop) -> None:
    fill_cart(client, shop)
    order_number = place(client, shop).json()["data"]["order_number"]
    intruder = auth_headers(create_user(db, email="intruder@testmail.in"))

    assert client.get(f"{ORDERS_URL}/{order_number}", headers=intruder).status_code == 404
    assert client.post(f"{ORDERS_URL}/{order_number}/cancel", headers=intruder).status_code == 404
    assert client.get(ORDERS_URL, headers=intruder).json()["data"] == []
    assert client.get(f"{ORDERS_URL}/{order_number}").status_code == 401
