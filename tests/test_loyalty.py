"""PureRoots Coins: cart toggle and clamping, checkout ledger rows, order hooks, API, admin adjustment,
online payment totals, expiry and the demo backfill."""
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.clock import utcnow
from app.core.exceptions import ConflictError
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.models.user import User
from app.scripts import backfill_loyalty
from app.services import loyalty_service, payment_service, upi_service
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
from tests.test_razorpay import FakeRazorpay, razorpay_api, razorpay_on  # noqa: F401 - fixtures
from tests.test_upi import upi_on  # noqa: F401 - fixture

CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
LOYALTY = "/api/v1/loyalty"


@dataclass
class Shop:
    user: User
    headers: dict[str, str]
    variant_id: str


@pytest.fixture
def shop(db: Session) -> Shop:
    category = create_category(db, name="Sweeteners", slug="sweeteners")
    jaggery = create_product(db, category, name="Organic Jaggery", slug="jaggery", tax_rate="5.00")
    variant = create_variant(db, jaggery, "1 kg", "500", "600", 1000, stock=100, is_default=True)
    user = create_user(db, email="asha@testmail.in")
    return Shop(user=user, headers=auth_headers(user), variant_id=str(variant.id))


def give_coins(client: TestClient, user: User, coins: int, note: str = "Welcome bonus") -> dict[str, Any]:
    response = client.post(
        f"/api/v1/internal/customers/{user.id}/coins",
        json={"coins": coins, "note": note, "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def set_quantity(client: TestClient, shop: Shop, quantity: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = shop.headers if headers is None else headers
    cart = client.get(CART, headers=headers).json()["data"]
    line = next((item for item in cart["items"] if item["variant_id"] == shop.variant_id), None)
    if line is None:
        response = client.post(f"{CART}/items", json={"variant_id": shop.variant_id, "quantity": quantity}, headers=headers)
    else:
        response = client.patch(f"{CART}/items/{line['id']}", json={"quantity": quantity}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def use_coins(client: TestClient, shop: Shop, use: bool = True):  # type: ignore[no-untyped-def]
    return client.post(f"{CART}/coins", json={"use": use}, headers=shop.headers)


def place(client: TestClient, shop: Shop, **overrides: Any) -> dict[str, Any]:
    response = client.post(ORDERS, json=checkout_payload(**overrides), headers=shop.headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def set_status(client: TestClient, order_id: str, status: str):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/internal/orders/{order_id}/status",
        json={"status": status, "note": "", "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )


def deliver(client: TestClient, order_id: str) -> None:
    for status in ("processing", "packed", "shipped", "delivered"):
        assert set_status(client, order_id, status).status_code == 200


def balances(db: Session, user: User) -> tuple[int, int]:
    db.expire_all()
    fresh = db.get(User, user.id)
    assert fresh is not None
    return fresh.coins_available, fresh.coins_pending


def ledger(db: Session, order_id: str | None = None, user: User | None = None) -> list[tuple[str, int, int, int]]:
    db.expire_all()
    stmt = select(LoyaltyTransaction).order_by(LoyaltyTransaction.created_at)
    if order_id is not None:
        stmt = stmt.where(LoyaltyTransaction.order_id == uuid.UUID(order_id))
    if user is not None:
        stmt = stmt.where(LoyaltyTransaction.user_id == user.id)
    return [(row.kind, row.coins, row.available_after, row.pending_after) for row in db.scalars(stmt)]


# --------------------------------------------------------------------------- cart


def test_cart_totals_for_signed_in_customer(client: TestClient, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    totals = set_quantity(client, shop, 3)["totals"]  # ₹1,500
    assert totals["loyalty_enabled"] is True
    assert totals["coins_available"] == 600
    assert totals["coins_pending"] == 0
    assert totals["coins_max_redeemable"] == 200
    assert totals["coins_redeem_fraction"] == "1/3"
    assert totals["coins_amount_to_next_step"] == 500.0
    assert totals["coins_blocked_reason"] is None
    assert totals["coins_use"] is False
    assert totals["coins_redeemed"] == 0
    assert totals["coins_to_earn"] == 450
    assert totals["total"] == 1500.0
    assert totals["coins_rule"].startswith("Use up to 1/3 of your coins for every ₹1,000")


def test_toggle_applies_max_and_earn_is_after_coins(client: TestClient, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    response = use_coins(client, shop)
    assert response.status_code == 200, response.text
    cart = response.json()["data"]
    totals = cart["totals"]
    assert totals["coins_use"] is True
    assert totals["coins_redeemed"] == 200
    assert totals["coins_discount"] == 200.0
    assert totals["total"] == 1300.0  # free shipping (threshold 999 on subtotal − coupon)
    assert totals["coins_to_earn"] == 390  # 30% of 1,300
    assert totals["tax_included"] == 61.9  # 1300 − 1300/1.05
    assert "Using 200 coins" in cart["messages"][0]

    totals = set_quantity(client, shop, 6)["totals"]  # ₹3,000 → the full balance
    assert (totals["coins_max_redeemable"], totals["coins_redeemed"], totals["total"]) == (600, 600, 2400.0)
    assert totals["coins_amount_to_next_step"] is None

    off = use_coins(client, shop, False).json()["data"]["totals"]
    assert (off["coins_use"], off["coins_redeemed"], off["total"]) == (False, 0, 3000.0)


def test_toggle_rejected_when_nothing_redeemable(client: TestClient, shop: Shop) -> None:
    set_quantity(client, shop, 3)
    response = use_coins(client, shop)  # no coins yet
    assert response.status_code == 422
    assert "don't have coins" in response.json()["error"]["message"]

    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 1)  # ₹500
    response = use_coins(client, shop)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == (
        "Add ₹500 more to use coins — you can use 1/3 of your coins for every ₹1,000."
    )
    assert client.get(CART, headers=shop.headers).json()["data"]["totals"]["coins_use"] is False


def test_redemption_is_clamped_when_cart_or_balance_change(client: TestClient, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 4)  # ₹2,000 → 2/3
    assert use_coins(client, shop).json()["data"]["totals"]["coins_redeemed"] == 400

    assert set_quantity(client, shop, 2)["totals"]["coins_redeemed"] == 200  # ₹1,000 → 1/3

    give_coins(client, shop.user, -300, "Correction")  # balance 300
    assert client.get(CART, headers=shop.headers).json()["data"]["totals"]["coins_redeemed"] == 100

    cart = set_quantity(client, shop, 1)  # ₹500: nothing usable → toggle turned off with a note
    assert cart["totals"]["coins_use"] is False
    assert cart["totals"]["coins_redeemed"] == 0
    assert any(message.startswith("Your coins were removed from this order.") for message in cart["messages"])
    # The note is shown once, not on every read.
    again = client.get(CART, headers=shop.headers).json()["data"]
    assert not any("coins were removed" in message for message in again["messages"])


def test_guest_cannot_redeem_but_sees_what_they_would_earn(client: TestClient, shop: Shop) -> None:
    guest: dict[str, str] = {}
    totals = set_quantity(client, shop, 3, headers=guest)["totals"]
    assert totals["coins_available"] == 0
    assert totals["coins_to_earn"] == 450
    assert totals["coins_blocked_reason"] is None
    response = client.post(f"{CART}/coins", json={"use": True})
    assert response.status_code == 401

    order = client.post(ORDERS, json=checkout_payload()).json()["data"]
    assert (order["coins_redeemed"], order["coins_earned"], order["coins_status"]) == (0, 0, None)


def test_guest_order_writes_no_ledger_rows(client: TestClient, db: Session, shop: Shop) -> None:
    set_quantity(client, shop, 3, headers={})
    order = client.post(ORDERS, json=checkout_payload()).json()["data"]
    assert order["coins_earned"] == 0
    assert ledger(db, order["id"]) == []


def test_loyalty_disabled(client: TestClient, shop: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    monkeypatch.setenv("LOYALTY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        totals = client.get(CART, headers=shop.headers).json()["data"]["totals"]
        assert totals["loyalty_enabled"] is False
        assert (totals["coins_max_redeemable"], totals["coins_to_earn"]) == (0, 0)
        assert use_coins(client, shop).status_code == 422
    finally:
        monkeypatch.delenv("LOYALTY_ENABLED")
        get_settings.cache_clear()


# --------------------------------------------------------------------------- checkout


def test_checkout_with_coupon_and_coins(client: TestClient, db: Session, shop: Shop) -> None:
    create_coupon(db, code="SAVE10", value="10")
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)  # ₹1,500
    assert client.post(f"{CART}/coupon", json={"code": "SAVE10"}, headers=shop.headers).status_code == 200
    totals = use_coins(client, shop).json()["data"]["totals"]
    # Coupon first (₹150), then coins on ₹1,350 → 1/3 of 600.
    assert (totals["coupon_discount"], totals["coins_redeemed"], totals["total"]) == (150.0, 200, 1150.0)
    assert totals["coins_to_earn"] == 345

    order = place(client, shop)
    assert order["subtotal"] == 1500.0
    assert order["discount_total"] == 150.0
    assert order["coins_redeemed"] == 200
    assert order["coins_earned"] == 345
    assert order["coins_status"] == "pending"
    assert order["grand_total"] == 1150.0
    assert order["shipping_fee"] == 0.0

    assert ledger(db, order["id"]) == [("redeem", -200, 400, 0), ("earn_pending", 345, 400, 345)]
    assert balances(db, shop.user) == (400, 345)
    me = client.get("/api/v1/auth/me", headers=shop.headers).json()["data"]
    assert (me["coins_available"], me["coins_pending"]) == (400, 345)

    stored = db.get(Order, uuid.UUID(order["id"]))
    assert stored is not None and stored.payments[0].amount == stored.grand_total
    # The cart is cleared; the toggle stays on for the next order.
    assert client.get(CART, headers=shop.headers).json()["data"]["items"] == []

    summary = client.get(ORDERS, headers=shop.headers).json()["data"][0]
    assert (summary["coins_redeemed"], summary["coins_earned"], summary["coins_status"]) == (200, 345, "pending")


def test_checkout_without_coins_still_earns(client: TestClient, db: Session, shop: Shop) -> None:
    set_quantity(client, shop, 1)  # ₹500 + ₹79 shipping
    order = place(client, shop)
    assert order["grand_total"] == 579.0
    assert order["coins_earned"] == 150  # shipping excluded
    assert ledger(db, order["id"]) == [("earn_pending", 150, 0, 150)]


def test_placement_rechecks_locked_balance(client: TestClient, db: Session, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    # Coins spent elsewhere between the cart and the click: the re-price at placement uses the fresh balance.
    give_coins(client, shop.user, -300, "Spent elsewhere")
    order = place(client, shop)
    assert order["coins_redeemed"] == 100
    assert balances(db, shop.user)[0] == 200

    # The ledger primitive itself refuses to overspend (the race-safe backstop).
    user = db.get(User, shop.user.id)
    stored = db.get(Order, uuid.UUID(order["id"]))
    assert user is not None and stored is not None
    with pytest.raises(ConflictError):
        loyalty_service.redeem_for_order(db, user.id, stored, 10_000)
    db.rollback()


def test_coins_covering_all_goods_need_cod(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay  # noqa: F811
) -> None:
    give_coins(client, shop.user, 5000)
    set_quantity(client, shop, 6)  # ₹3,000 → coins capped at ₹3,000, free shipping → total 0
    totals = use_coins(client, shop).json()["data"]["totals"]
    assert (totals["coins_redeemed"], totals["total"]) == (3000, 0.0)
    response = client.post(ORDERS, json=checkout_payload(payment_method="razorpay"), headers=shop.headers)
    assert response.status_code == 422
    assert "Cash on Delivery" in response.json()["error"]["message"]
    order = place(client, shop)
    assert (order["grand_total"], order["coins_redeemed"], order["coins_earned"]) == (0.0, 3000, 0)


def test_coins_with_paid_shipping(client: TestClient, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 2)
    use_coins(client, shop)
    order = place(client, shop, delivery_method="express")
    # ₹1,000 − 200 coins + ₹149 express: shipping is always paid.
    assert (order["coins_redeemed"], order["shipping_fee"], order["grand_total"]) == (200, 149.0, 949.0)


# --------------------------------------------------------------------------- order hooks


def test_delivered_confirms_earn_idempotently(client: TestClient, db: Session, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    order = place(client, shop)
    deliver(client, order["id"])
    assert balances(db, shop.user) == (400 + 390, 0)
    assert ledger(db, order["id"])[-1] == ("earn_confirmed", 390, 790, 0)

    detail = client.get(f"{ORDERS}/{order['order_number']}", headers=shop.headers).json()["data"]
    assert detail["coins_status"] == "credited"

    stored = db.get(Order, uuid.UUID(order["id"]))
    assert stored is not None
    assert loyalty_service.confirm_for_order(db, stored) is False
    db.commit()
    assert len(ledger(db, order["id"])) == 3


def test_customer_cancel_returns_coins_and_reverses_pending(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory
) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    order = place(client, shop)
    response = client.post(f"{ORDERS}/{order['order_number']}/cancel", headers=shop.headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["coins_status"] == "reversed"
    assert balances(db, shop.user) == (600, 0)
    assert [row[0] for row in ledger(db, order["id"])] == ["redeem", "earn_pending", "redeem_reversed", "earn_reversed"]
    assert ("release", order["id"]) in inventory.calls

    stored = db.get(Order, uuid.UUID(order["id"]))
    assert stored is not None
    assert loyalty_service.reverse_for_order(db, stored) is False  # idempotent
    db.commit()
    assert balances(db, shop.user) == (600, 0)


def test_refund_after_delivery_can_leave_negative_balance(client: TestClient, db: Session, shop: Shop) -> None:
    set_quantity(client, shop, 3)
    order = place(client, shop)  # earns 450
    deliver(client, order["id"])
    assert balances(db, shop.user) == (450, 0)
    give_coins(client, shop.user, -400, "Spent on another order")  # 50 left

    assert set_status(client, order["id"], "refunded").status_code == 200
    assert balances(db, shop.user) == (-400, 0)
    rows = db.scalars(
        select(LoyaltyTransaction).where(
            LoyaltyTransaction.order_id == uuid.UUID(order["id"]), LoyaltyTransaction.kind == "earn_reversed"
        )
    ).all()
    assert len(rows) == 1 and rows[0].coins == -450 and rows[0].available_after == -400
    assert "already been spent" in rows[0].note and "negative" in rows[0].note

    # Redemption is blocked until the balance is positive again.
    set_quantity(client, shop, 6)
    totals = client.get(CART, headers=shop.headers).json()["data"]["totals"]
    assert totals["coins_max_redeemable"] == 0
    assert "below zero" in totals["coins_blocked_reason"]
    assert use_coins(client, shop).status_code == 422


def test_cancelled_then_refunded_does_not_reverse_twice(client: TestClient, db: Session, shop: Shop) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    order = place(client, shop)
    assert set_status(client, order["id"], "cancelled").status_code == 200
    stored = db.get(Order, uuid.UUID(order["id"]))
    assert stored is not None
    loyalty_service.on_status_change(db, stored, "refunded")
    db.commit()
    assert balances(db, shop.user) == (600, 0)
    assert len(ledger(db, order["id"])) == 4


# --------------------------------------------------------------------------- online payments and expiry


def test_razorpay_amount_is_after_coins(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay  # noqa: F811
) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    order = place(client, shop, payment_method="razorpay")
    assert order["grand_total"] == 1300.0
    assert order["payment"]["amount"] == 130000
    created = [body for method, path, body in razorpay_on.requests if method == "POST" and path == "/orders"]
    assert created[-1]["amount"] == 130000
    assert balances(db, shop.user) == (400, 390)

    # Not paid in time → cancelled by the sweep → coins come back.
    stored = db.get(Order, uuid.UUID(order["id"]))
    assert stored is not None
    stored.placed_at = utcnow() - timedelta(minutes=45)
    db.commit()
    assert payment_service.expire_unpaid_orders(db, inventory, get_settings()) == 1
    assert balances(db, shop.user) == (600, 0)
    assert [row[0] for row in ledger(db, order["id"])][-2:] == ["redeem_reversed", "earn_reversed"]


def test_upi_amount_is_after_coins_and_expiry_returns_coins(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, upi_on: None  # noqa: F811
) -> None:
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 6)
    use_coins(client, shop)
    order = place(client, shop, payment_method="upi")
    assert order["grand_total"] == 2400.0
    assert order["payment"]["amount"] == 2400.0
    assert "am=2400.00" in order["payment"]["upi_uri"]
    assert balances(db, shop.user) == (0, 720)

    settings = get_settings()
    later = utcnow() + timedelta(minutes=settings.upi_payment_expiry_minutes + 1)
    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=later) == 1
    assert balances(db, shop.user) == (600, 0)
    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=later) == 0
    assert balances(db, shop.user) == (600, 0)


# --------------------------------------------------------------------------- API


def test_loyalty_endpoint(client: TestClient, shop: Shop) -> None:
    assert client.get(LOYALTY).status_code == 401
    give_coins(client, shop.user, 600)
    set_quantity(client, shop, 3)
    use_coins(client, shop)
    order = place(client, shop)
    deliver(client, order["id"])

    body = client.get(LOYALTY, params={"page_size": 2}, headers=shop.headers).json()
    data = body["data"]
    assert (data["available"], data["pending"]) == (790, 0)
    assert data["lifetime_earned"] == 600 + 390
    assert data["rule"] == {
        "enabled": True,
        "earn_percent": 30,
        "step_amount": 1000.0,
        "max_steps": 3,
        "text": "Use up to 1/3 of your coins for every ₹1,000 you spend — all of them from ₹3,000. 1 coin = ₹1.",
    }
    assert body["meta"] == {"page": 1, "page_size": 2, "total": 4, "total_pages": 2}
    newest = data["transactions"][0]
    assert newest["kind"] == "earn_confirmed"
    assert newest["kind_label"] == "Coins credited"
    assert newest["coins"] == 390
    assert newest["order_number"] == order["order_number"]
    page2 = client.get(LOYALTY, params={"page": 2, "page_size": 2}, headers=shop.headers).json()["data"]
    assert [row["kind"] for row in page2["transactions"]] == ["redeem", "adjustment"]
    assert page2["transactions"][1]["order_number"] is None


def test_internal_adjustment(client: TestClient, db: Session, shop: Shop) -> None:
    data = give_coins(client, shop.user, 250, "Goodwill for a late delivery")
    assert (data["available"], data["pending"]) == (250, 0)
    assert data["transaction"]["kind"] == "adjustment"
    assert data["transaction"]["created_by"] == "admin:priya"
    assert data["transaction"]["note"] == "Goodwill for a late delivery"

    url = f"/api/v1/internal/customers/{shop.user.id}/coins"
    too_much = client.post(url, json={"coins": -300, "note": "Oops", "changed_by": "admin:priya"}, headers=INTERNAL_HEADERS)
    assert too_much.status_code == 409
    assert client.post(url, json={"coins": 0, "note": "Zero", "changed_by": "admin:priya"}, headers=INTERNAL_HEADERS).status_code == 422
    assert client.post(url, json={"coins": 5, "note": "", "changed_by": "admin:priya"}, headers=INTERNAL_HEADERS).status_code == 422
    assert client.post(url, json={"coins": 200_000, "note": "Huge", "changed_by": "admin:priya"}, headers=INTERNAL_HEADERS).status_code == 422
    unknown = f"/api/v1/internal/customers/{uuid.uuid4()}/coins"
    assert client.post(unknown, json={"coins": 5, "note": "Hello", "changed_by": "admin:priya"}, headers=INTERNAL_HEADERS).status_code == 404
    assert client.post(url, json={"coins": 5, "note": "Hello", "changed_by": "admin:priya"}).status_code == 401
    assert balances(db, shop.user) == (250, 0)


def test_auth_me_and_login_include_balances(client: TestClient, shop: Shop) -> None:
    give_coins(client, shop.user, 42)
    me = client.get("/api/v1/auth/me", headers=shop.headers).json()["data"]
    assert (me["coins_available"], me["coins_pending"]) == (42, 0)
    login = client.post("/api/v1/auth/login", json={"email": "asha@testmail.in", "password": "Secret123"}).json()
    assert login["data"]["user"]["coins_available"] == 42


# --------------------------------------------------------------------------- backfill


def test_backfill_is_idempotent(client: TestClient, db: Session, shop: Shop) -> None:
    other = create_user(db, email="ravi@testmail.in")
    orders = {}
    for name in ("delivered", "open", "cancelled"):
        set_quantity(client, shop, 3)
        orders[name] = place(client, shop)
    deliver(client, orders["delivered"]["id"])
    assert set_status(client, orders["cancelled"]["id"], "cancelled").status_code == 200
    # Pretend these orders predate the feature.
    for row in db.scalars(select(LoyaltyTransaction)).all():
        db.delete(row)
    for order in db.scalars(select(Order)).all():
        order.coins_earned = 0
    user = db.get(User, shop.user.id)
    assert user is not None
    user.coins_available = user.coins_pending = 0
    db.commit()

    settings = get_settings()
    stats = backfill_loyalty.backfill(db, settings)
    assert (stats["orders"], stats["credited"], stats["pending"]) == (2, 1, 1)
    assert balances(db, shop.user) == (450, 450)
    assert balances(db, other) == (0, 0)
    assert ledger(db, orders["cancelled"]["id"]) == []
    assert [row[0] for row in ledger(db, orders["delivered"]["id"])] == ["earn_pending", "earn_confirmed"]

    again = backfill_loyalty.backfill(db, settings)
    assert again["orders"] == 0
    assert balances(db, shop.user) == (450, 450)

    # The open order then follows the normal hooks.
    deliver(client, orders["open"]["id"])
    assert balances(db, shop.user) == (900, 0)
