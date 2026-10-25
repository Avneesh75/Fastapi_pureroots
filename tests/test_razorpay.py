"""Razorpay: provider helpers, checkout, verify, webhook, pay-retry, refunds and the expiry sweep.

Razorpay's REST API is replaced by ``FakeRazorpay`` through ``httpx.MockTransport`` — no network.
"""
import base64
import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.clock import utcnow
from app.models.order import Order
from app.models.user import User
from app.services import payment_service
from app.services.payments import razorpay as razorpay_module
from app.services.payments.razorpay import RazorpayProvider, sanitize_entity, to_paise
from tests.conftest import INTERNAL_HEADERS
from tests.factories import auth_headers, checkout_payload, create_category, create_product, create_user, create_variant
from tests.fakes import FakeInventory

KEY_ID = "rzp_test_PureRootsKey01"
KEY_SECRET = "test-key-secret-never-shown"
WEBHOOK_SECRET = "test-webhook-secret"
ORDERS_URL = "/api/v1/orders"
VERIFY_URL = "/api/v1/payments/razorpay/verify"
WEBHOOK_URL = "/api/v1/payments/razorpay/webhook"
OPTIONS_URL = "/api/v1/checkout/options"
RAZORPAY_ENV = {
    "RAZORPAY_KEY_ID": KEY_ID,
    "RAZORPAY_KEY_SECRET": KEY_SECRET,
    "RAZORPAY_WEBHOOK_SECRET": WEBHOOK_SECRET,
}


# --------------------------------------------------------------------------- fake Razorpay API


class FakeRazorpay:
    """In-memory Razorpay REST API (orders, payments, refunds) behind ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.refunds: list[dict[str, Any]] = []
        self.fail_create: int | None = None  # HTTP status to return for POST /orders
        self.fail_refund: int | None = None
        self.network_down = False
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_TEST{self._seq:06d}"

    def add_payment(self, order_id: str, *, amount: int | None = None, status: str = "captured", currency: str = "INR") -> str:
        payment_id = self._id("pay")
        self.payments[payment_id] = {
            "id": payment_id,
            "entity": "payment",
            "order_id": order_id,
            "amount": self.orders[order_id]["amount"] if amount is None else amount,
            "currency": currency,
            "status": status,
            "method": "upi",
            "email": "secret@example.com",
            "contact": "+919876543210",
            "vpa": "asha@okbank",
            "card": {"last4": "1111"},
        }
        if status == "captured":
            self.orders[order_id]["status"] = "paid"
        return payment_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.network_down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content or b"{}") if request.content else {}
        path = request.url.path.removeprefix("/v1")
        self.requests.append((request.method, path, body))
        expected_auth = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected_auth:
            return httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}})
        parts = path.strip("/").split("/")
        if request.method == "POST" and parts == ["orders"]:
            if self.fail_create:
                return httpx.Response(self.fail_create, json={"error": {"code": "SERVER_ERROR", "description": "boom"}})
            order_id = self._id("order")
            self.orders[order_id] = {"id": order_id, "entity": "order", "status": "created", "attempts": 0, **body}
            return httpx.Response(200, json=self.orders[order_id])
        if request.method == "GET" and len(parts) == 2 and parts[0] == "orders":
            order = self.orders.get(parts[1])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={"error": {"code": "BAD_REQUEST_ERROR"}})
        if request.method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "payments":
            items = [p for p in self.payments.values() if p["order_id"] == parts[1]]
            return httpx.Response(200, json={"entity": "collection", "count": len(items), "items": items})
        if request.method == "GET" and len(parts) == 2 and parts[0] == "payments":
            payment = self.payments.get(parts[1])
            return httpx.Response(200, json=payment) if payment else httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR"}})
        if request.method == "POST" and len(parts) == 3 and parts[0] == "payments" and parts[2] == "capture":
            payment = self.payments[parts[1]]
            payment["status"] = "captured"
            return httpx.Response(200, json=payment)
        if request.method == "POST" and len(parts) == 3 and parts[0] == "payments" and parts[2] == "refund":
            if self.fail_refund:
                return httpx.Response(
                    self.fail_refund,
                    json={"error": {"code": "BAD_REQUEST_ERROR", "description": "The payment has been fully refunded already"}},
                )
            refund = {"id": self._id("rfnd"), "entity": "refund", "payment_id": parts[1], "amount": body["amount"], "status": "processed"}
            self.refunds.append(refund)
            return httpx.Response(200, json=refund)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})


@pytest.fixture
def razorpay_api(monkeypatch: pytest.MonkeyPatch) -> FakeRazorpay:
    fake = FakeRazorpay()
    monkeypatch.setattr(razorpay_module, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def razorpay_on(razorpay_api: FakeRazorpay) -> Iterator[FakeRazorpay]:
    """Configure the Razorpay keys (settings are cached, so clear the cache on both sides)."""
    previous = {name: os.environ.get(name) for name in RAZORPAY_ENV}
    os.environ.update(RAZORPAY_ENV)
    get_settings.cache_clear()
    try:
        yield razorpay_api
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


# --------------------------------------------------------------------------- shop helpers


@dataclass
class Shop:
    user: User
    headers: dict[str, str]
    variant_id: str


@pytest.fixture
def shop(db: Session) -> Shop:
    category = create_category(db, name="Ghee & Oils", slug="ghee")
    ghee = create_product(db, category, name="A2 Desi Cow Ghee", slug="a2-ghee", tax_rate="12.00")
    variant = create_variant(db, ghee, "500 ml", "799", "899", 500, stock=30, is_default=True)
    user = create_user(db, email="asha@testmail.in")
    return Shop(user=user, headers=auth_headers(user), variant_id=str(variant.id))


def fill_cart(client: TestClient, shop: Shop, quantity: int = 1, headers: dict[str, str] | None = None) -> None:
    response = client.post(
        "/api/v1/cart/items",
        json={"variant_id": shop.variant_id, "quantity": quantity},
        headers=shop.headers if headers is None else headers,
    )
    assert response.status_code == 200, response.text


def place_razorpay(client: TestClient, shop: Shop, quantity: int = 1, headers: dict[str, str] | None = None) -> dict[str, Any]:
    fill_cart(client, shop, quantity, headers)
    response = client.post(
        ORDERS_URL, json=checkout_payload(payment_method="razorpay"), headers=shop.headers if headers is None else headers
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def signature(order_id: str, payment_id: str, secret: str = KEY_SECRET) -> str:
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def verify_body(order: dict[str, Any], payment_id: str, **overrides: Any) -> dict[str, Any]:
    rzp_order_id = order["payment"]["razorpay_order_id"]
    body = {
        "order_number": order["order_number"],
        "razorpay_order_id": rzp_order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature(rzp_order_id, payment_id),
    }
    body.update(overrides)
    return body


def webhook(client: TestClient, event: str, payment: dict[str, Any], secret: str = WEBHOOK_SECRET, **kwargs: Any):  # type: ignore[no-untyped-def]
    raw = json.dumps({"entity": "event", "event": event, "payload": {"payment": {"entity": payment}}}).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        WEBHOOK_URL, content=raw, headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig}, **kwargs
    )


def paid_order(client: TestClient, shop: Shop, fake: FakeRazorpay, quantity: int = 1) -> tuple[dict[str, Any], str]:
    order = place_razorpay(client, shop, quantity)
    payment_id = fake.add_payment(order["payment"]["razorpay_order_id"])
    response = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"], payment_id


def set_status(client: TestClient, order_id: str, status: str):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/internal/orders/{order_id}/status",
        json={"status": status, "note": "", "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )


def db_order(db: Session, order_number: str) -> Order:
    db.expire_all()
    order = db.query(Order).filter(Order.order_number == order_number).one()
    return order


# --------------------------------------------------------------------------- provider helpers


def test_to_paise_rounds_half_up() -> None:
    assert to_paise("878") == 87800
    assert to_paise("199.995") == 20000
    assert to_paise(0.1) == 10


def test_payment_signature_helper() -> None:
    provider = RazorpayProvider(KEY_ID, KEY_SECRET, WEBHOOK_SECRET)
    good = signature("order_A", "pay_B")
    assert provider.verify_payment_signature("order_A", "pay_B", good)
    assert not provider.verify_payment_signature("order_A", "pay_C", good)
    assert not provider.verify_payment_signature("order_A", "pay_B", signature("order_A", "pay_B", "other-secret"))
    assert not provider.verify_payment_signature("order_A", "pay_B", "")
    assert not RazorpayProvider(KEY_ID, "", "").verify_payment_signature("order_A", "pay_B", good)


def test_webhook_signature_helper() -> None:
    provider = RazorpayProvider(KEY_ID, KEY_SECRET, WEBHOOK_SECRET)
    body = b'{"event":"payment.captured"}'
    good = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert provider.verify_webhook_signature(body, good)
    assert not provider.verify_webhook_signature(body + b" ", good)
    assert not provider.verify_webhook_signature(body, None)
    assert not RazorpayProvider(KEY_ID, KEY_SECRET, "").verify_webhook_signature(body, good)


def test_secret_never_in_repr_and_entities_are_sanitized() -> None:
    provider = RazorpayProvider(KEY_ID, KEY_SECRET, WEBHOOK_SECRET)
    assert KEY_SECRET not in repr(provider) and WEBHOOK_SECRET not in repr(provider)
    clean = sanitize_entity({"id": "pay_1", "amount": 100, "email": "a@b.in", "contact": "+91", "vpa": "x@y", "card": {}})
    assert clean == {"id": "pay_1", "amount": 100}
    assert KEY_SECRET not in repr(get_settings())


def test_provider_maps_network_and_http_errors(razorpay_api: FakeRazorpay) -> None:
    provider = RazorpayProvider(KEY_ID, KEY_SECRET, transport=httpx.MockTransport(razorpay_api.handler))
    razorpay_api.network_down = True
    with pytest.raises(razorpay_module.RazorpayAPIError) as network:
        provider.fetch_payment("pay_X")
    assert network.value.status_code == 503 and network.value.http_status is None
    razorpay_api.network_down = False
    with pytest.raises(razorpay_module.RazorpayAPIError) as client_error:
        provider.fetch_payment("pay_missing")
    assert client_error.value.is_client_error and client_error.value.http_status == 400

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    slow = RazorpayProvider(KEY_ID, KEY_SECRET, transport=httpx.MockTransport(timeout))
    with pytest.raises(razorpay_module.RazorpayAPIError) as timed_out:
        slow.fetch_payment("pay_X")
    assert "too long" in timed_out.value.message


# --------------------------------------------------------------------------- options


def test_options_hide_razorpay_when_not_configured(client: TestClient) -> None:
    methods = {m["code"]: m for m in client.get(OPTIONS_URL).json()["data"]["payment_methods"]}
    assert "razorpay" not in methods
    assert methods["cod"]["enabled"] is True
    assert "online" not in methods  # no disabled "coming soon" placeholder any more
    assert all(m["enabled"] for m in methods.values())


def test_options_show_razorpay_when_configured(client: TestClient, razorpay_on: FakeRazorpay) -> None:
    methods = {m["code"]: m for m in client.get(OPTIONS_URL).json()["data"]["payment_methods"]}
    assert methods["razorpay"]["enabled"] is True
    assert "Razorpay" in methods["razorpay"]["description"]
    assert methods["razorpay"]["label"] == "Cards, netbanking & wallets (Razorpay)"
    assert methods["cod"]["enabled"] is True
    assert "online" not in methods  # the "coming soon" placeholder goes away


def test_razorpay_rejected_at_checkout_when_not_configured(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    response = client.post(ORDERS_URL, json=checkout_payload(payment_method="razorpay"), headers=shop.headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "payment_method"


# --------------------------------------------------------------------------- checkout


def test_checkout_creates_razorpay_order_in_paise(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop, quantity=1)  # 799 + 79 shipping

    assert order["status"] == "pending"
    assert order["payment_status"] == "pending"
    assert order["payment_method"] == "razorpay"
    assert order["can_pay"] is True
    payment = order["payment"]
    assert payment["provider"] == "razorpay"
    assert payment["key_id"] == KEY_ID
    assert payment["amount"] == 87800
    assert payment["currency"] == "INR"
    assert payment["name"] == "PureRoots Aahar"
    assert payment["order_number"] == order["order_number"]
    assert payment["prefill"] == {"name": "Asha Kulkarni", "email": "asha@testmail.in", "contact": "+919876543210"}

    method, path, body = razorpay_on.requests[0]
    assert (method, path) == ("POST", "/orders")
    assert body == {
        "amount": 87800,
        "currency": "INR",
        "receipt": order["order_number"],
        "notes": {"order_id": order["id"], "order_number": order["order_number"]},
    }
    assert payment["razorpay_order_id"] in razorpay_on.orders

    stored = db_order(db, order["order_number"])
    assert [(p.provider, p.provider_reference, p.status, str(p.amount)) for p in stored.payments] == [
        ("razorpay", payment["razorpay_order_id"], "created", "878.00")
    ]
    assert [h.to_status for h in stored.status_history] == ["pending"]
    assert inventory.calls == [("reserve", order["id"])]
    assert client.get("/api/v1/cart", headers=shop.headers).json()["data"]["items"] == []
    assert KEY_SECRET not in json.dumps(order)


@pytest.mark.parametrize("failure", ["http500", "http400", "network"])
def test_checkout_razorpay_failure_releases_stock_and_rolls_back(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay, failure: str
) -> None:
    if failure == "network":
        razorpay_on.network_down = True
    else:
        razorpay_on.fail_create = 500 if failure == "http500" else 400
    fill_cart(client, shop)
    response = client.post(ORDERS_URL, json=checkout_payload(payment_method="razorpay"), headers=shop.headers)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert "nothing was charged" in error["message"]
    assert [call[0] for call in inventory.calls] == ["reserve", "release"]
    assert db.query(Order).count() == 0
    assert len(client.get("/api/v1/cart", headers=shop.headers).json()["data"]["items"]) == 1


def test_cod_unaffected_when_razorpay_enabled(
    client: TestClient, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    fill_cart(client, shop, 2)
    response = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers)
    assert response.status_code == 201, response.text
    order = response.json()["data"]
    assert (order["status"], order["payment_status"], order["payment_method"]) == ("confirmed", "cod_pending", "cod")
    assert order["payment"] is None and order["can_pay"] is False
    assert razorpay_on.requests == []

    cancelled = client.post(f"{ORDERS_URL}/{order['order_number']}/cancel", headers=shop.headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["payment_status"] == "cod_pending"
    assert razorpay_on.requests == []
    assert client.post(f"{ORDERS_URL}/{order['order_number']}/pay", json={}, headers=shop.headers).status_code == 409


# --------------------------------------------------------------------------- verify


def test_verify_success_confirms_order_and_is_idempotent(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])

    response = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["status"], data["payment_status"], data["can_pay"]) == ("confirmed", "paid", False)
    history = [(h["from_status"], h["to_status"], h["note"], h["changed_by"]) for h in data["status_history"]]
    assert history[-1] == ("pending", "confirmed", "Payment received via Razorpay", "system")

    stored = db_order(db, order["order_number"])
    row = stored.payments[0]
    assert row.status == "paid"
    assert row.raw_response["razorpay_payment_id"] == payment_id
    raw = json.dumps(row.raw_response)
    assert "secret@example.com" not in raw and "asha@okbank" not in raw and "last4" not in raw

    again = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert again.status_code == 200
    assert len(again.json()["data"]["status_history"]) == len(data["status_history"])


def test_verify_bad_signature_leaves_order_untouched(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    response = client.post(
        VERIFY_URL, json=verify_body(order, payment_id, razorpay_signature="0" * 64), headers=shop.headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PAYMENT_VERIFICATION_FAILED"
    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status, stored.payments[0].status) == ("pending", "pending", "created")


def test_verify_mismatched_order_is_rejected(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    other = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(other["payment"]["razorpay_order_id"])
    # A valid signature for the OTHER order's Razorpay order, presented for this order.
    body = verify_body(other, payment_id, order_number=order["order_number"])
    response = client.post(VERIFY_URL, json=body, headers=shop.headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PAYMENT_VERIFICATION_FAILED"
    # A payment that belongs to another Razorpay order, signed against this one.
    rzp_order_id = order["payment"]["razorpay_order_id"]
    forged = verify_body(order, payment_id)
    assert forged["razorpay_order_id"] == rzp_order_id
    response = client.post(VERIFY_URL, json=forged, headers=shop.headers)
    assert response.status_code == 400
    assert db_order(db, order["order_number"]).status == "pending"


def test_verify_amount_mismatch_is_rejected(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"], amount=100)
    response = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "PAYMENT_VERIFICATION_FAILED"
    assert any(detail["field"] == "amount" for detail in error["details"])
    assert db_order(db, order["order_number"]).payment_status == "pending"


def test_verify_captures_authorized_payment(client: TestClient, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"], status="authorized")
    response = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert response.status_code == 200
    assert response.json()["data"]["payment_status"] == "paid"
    assert ("POST", f"/payments/{payment_id}/capture", {"amount": 87800, "currency": "INR"}) in razorpay_on.requests


def test_verify_for_guest_requires_order_email(client: TestClient, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop, headers={})
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    denied = client.post(VERIFY_URL, json=verify_body(order, payment_id, email="someone@else.in"))
    assert denied.status_code == 404
    other_user = client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers)
    assert other_user.status_code == 404  # the order is a guest order, not Asha's account order
    allowed = client.post(VERIFY_URL, json=verify_body(order, payment_id, email="ASHA@testmail.in"))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data"]["payment_status"] == "paid"


# --------------------------------------------------------------------------- webhook


def test_webhook_payment_captured_marks_paid(client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    entity = razorpay_on.payments[payment_id]

    response = webhook(client, "payment.captured", entity)
    assert response.status_code == 200
    assert response.json()["data"] == {"event": "payment.captured", "handled": True}
    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status) == ("confirmed", "paid")

    # Retries of the same event, a later order.paid and the browser's verify are all no-ops.
    assert webhook(client, "payment.captured", entity).status_code == 200
    assert webhook(client, "order.paid", entity).status_code == 200
    assert client.post(VERIFY_URL, json=verify_body(order, payment_id), headers=shop.headers).status_code == 200
    stored = db_order(db, order["order_number"])
    assert [h.to_status for h in stored.status_history] == ["pending", "confirmed"]


def test_webhook_order_paid_marks_paid(client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    assert webhook(client, "order.paid", razorpay_on.payments[payment_id]).status_code == 200
    assert db_order(db, order["order_number"]).payment_status == "paid"


def test_webhook_invalid_signature_is_400(client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    response = webhook(client, "payment.captured", razorpay_on.payments[payment_id], secret="wrong")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SIGNATURE"
    missing = client.post(WEBHOOK_URL, content=b"{}", headers={"Content-Type": "application/json"})
    assert missing.status_code == 400
    assert db_order(db, order["order_number"]).payment_status == "pending"


def test_webhook_is_exempt_from_csrf_header(raw_client: TestClient, razorpay_on: FakeRazorpay) -> None:
    raw_client.cookies.set("pr_cart", "a" * 32)  # a cookie that would normally require X-Requested-With
    entity = {"id": "pay_unknown", "order_id": "order_unknown", "amount": 100, "currency": "INR", "status": "captured"}
    response = webhook(raw_client, "payment.captured", entity)
    assert response.status_code == 200
    assert response.json()["data"]["handled"] is False


def test_webhook_payment_failed_keeps_order_pending_and_retry_reuses_razorpay_order(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    rzp_order_id = order["payment"]["razorpay_order_id"]
    failed = {
        "id": "pay_FAILED01",
        "order_id": rzp_order_id,
        "amount": 87800,
        "currency": "INR",
        "status": "failed",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment was declined by the bank",
    }
    razorpay_on.orders[rzp_order_id]["status"] = "attempted"
    response = webhook(client, "payment.failed", failed)
    assert response.status_code == 200 and response.json()["data"]["handled"] is True

    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status, stored.payments[0].status) == ("pending", "failed", "failed")
    assert stored.payments[0].raw_response["last_error"]["error_description"] == "Payment was declined by the bank"

    detail = client.get(f"{ORDERS_URL}/{order['order_number']}", headers=shop.headers).json()["data"]
    assert detail["can_pay"] is True
    retry = client.post(f"{ORDERS_URL}/{order['order_number']}/pay", json={}, headers=shop.headers)
    assert retry.status_code == 200, retry.text
    data = retry.json()["data"]
    assert data["payment"]["razorpay_order_id"] == rzp_order_id  # reused
    assert data["payment_status"] == "pending"
    assert len([r for r in razorpay_on.requests if r[:2] == ("POST", "/orders")]) == 1


# --------------------------------------------------------------------------- pay retry


def test_pay_retry_creates_new_razorpay_order_when_old_one_is_unusable(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    old_id = order["payment"]["razorpay_order_id"]
    del razorpay_on.orders[old_id]["amount"]  # e.g. an order Razorpay no longer reports as reusable
    razorpay_on.orders[old_id]["status"] = "expired"

    retry = client.post(f"{ORDERS_URL}/{order['order_number']}/pay", json={}, headers=shop.headers)
    assert retry.status_code == 200, retry.text
    new_payment = retry.json()["data"]["payment"]
    assert new_payment["razorpay_order_id"] != old_id
    assert new_payment["amount"] == 87800
    assert len(db_order(db, order["order_number"]).payments) == 2

    # Paying the new Razorpay order confirms the order.
    payment_id = razorpay_on.add_payment(new_payment["razorpay_order_id"])
    body = verify_body({"order_number": order["order_number"], "payment": new_payment}, payment_id)
    assert client.post(VERIFY_URL, json=body, headers=shop.headers).json()["data"]["status"] == "confirmed"


def test_pay_retry_guest_access_by_email(client: TestClient, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop, headers={})
    url = f"{ORDERS_URL}/{order['order_number']}/pay"
    assert client.post(url, json={}).status_code == 404
    assert client.post(url, json={"email": "nope@testmail.in"}).status_code == 404
    response = client.post(url, json={"email": "asha@testmail.in"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["payment"]["order_number"] == order["order_number"]


def test_pay_retry_conflicts_when_not_payable(client: TestClient, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    paid, _ = paid_order(client, shop, razorpay_on)
    response = client.post(f"{ORDERS_URL}/{paid['order_number']}/pay", json={}, headers=shop.headers)
    assert response.status_code == 409
    assert "already been paid" in response.json()["error"]["message"]

    pending = place_razorpay(client, shop)
    client.post(f"{ORDERS_URL}/{pending['order_number']}/cancel", headers=shop.headers)
    assert client.post(f"{ORDERS_URL}/{pending['order_number']}/pay", json={}, headers=shop.headers).status_code == 409


def test_pay_retry_detects_payment_made_meanwhile(
    client: TestClient, db: Session, shop: Shop, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    razorpay_on.add_payment(order["payment"]["razorpay_order_id"])  # paid, but the webhook never arrived
    response = client.post(f"{ORDERS_URL}/{order['order_number']}/pay", json={}, headers=shop.headers)
    assert response.status_code == 409
    assert db_order(db, order["order_number"]).payment_status == "paid"


def test_unpaid_razorpay_order_cannot_be_confirmed_by_admin(client: TestClient, shop: Shop, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    response = set_status(client, order["id"], "confirmed")
    assert response.status_code == 409
    assert "waiting for its online payment" in response.json()["error"]["message"]


# --------------------------------------------------------------------------- refunds


def test_customer_cancel_of_paid_order_refunds_first(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order, payment_id = paid_order(client, shop, razorpay_on)
    response = client.post(f"{ORDERS_URL}/{order['order_number']}/cancel", headers=shop.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["status"], data["payment_status"]) == ("cancelled", "refunded")
    assert "Refund of ₹878.00 issued via Razorpay" in data["status_history"][-1]["note"]

    assert razorpay_on.refunds == [
        {"id": razorpay_on.refunds[0]["id"], "entity": "refund", "payment_id": payment_id, "amount": 87800, "status": "processed"}
    ]
    row = db_order(db, order["order_number"]).payments[0]
    assert row.status == "refunded"
    assert row.raw_response["refunds"][0]["id"] == razorpay_on.refunds[0]["id"]
    assert ("release", order["id"]) in inventory.calls


@pytest.mark.parametrize(("failure", "status_code"), [("rejected", 409), ("network", 503)])
def test_refund_failure_blocks_cancellation(
    client: TestClient,
    db: Session,
    shop: Shop,
    inventory: FakeInventory,
    razorpay_on: FakeRazorpay,
    failure: str,
    status_code: int,
) -> None:
    order, _ = paid_order(client, shop, razorpay_on)
    if failure == "rejected":
        razorpay_on.fail_refund = 400
    else:
        razorpay_on.network_down = True
    response = set_status(client, order["id"], "cancelled")
    assert response.status_code == status_code
    error = response.json()["error"]
    assert error["code"] == ("REFUND_FAILED" if failure == "rejected" else "SERVICE_UNAVAILABLE")
    assert "not changed" in error["message"]

    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status, stored.payments[0].status) == ("confirmed", "paid", "paid")
    assert ("release", order["id"]) not in inventory.calls


def test_delivered_paid_order_refund_goes_through_razorpay(
    client: TestClient, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order, payment_id = paid_order(client, shop, razorpay_on)
    for status in ("processing", "packed", "shipped", "delivered"):
        assert set_status(client, order["id"], status).status_code == 200
    response = set_status(client, order["id"], "refunded")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["payment_status"] == "refunded"
    assert razorpay_on.refunds[0]["payment_id"] == payment_id
    assert ("return", order["id"]) in inventory.calls


# --------------------------------------------------------------------------- expiry


def _age(db: Session, order_number: str, minutes: int) -> None:
    order = db_order(db, order_number)
    order.placed_at = utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_expiry_sweep_cancels_unpaid_orders_and_releases_stock(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    old = place_razorpay(client, shop)
    fresh = place_razorpay(client, shop)
    fill_cart(client, shop)
    cod = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers).json()["data"]
    _age(db, old["order_number"], 31)
    _age(db, cod["order_number"], 120)

    settings = get_settings()
    assert payment_service.expire_unpaid_orders(db, inventory, settings) == 1
    stored = db_order(db, old["order_number"])
    assert stored.status == "cancelled"
    assert stored.status_history[-1].note == "Payment not completed in time"
    assert stored.status_history[-1].changed_by == "system"
    assert ("release", old["id"]) in inventory.calls
    assert db_order(db, fresh["order_number"]).status == "pending"
    assert db_order(db, cod["order_number"]).status == "confirmed"

    # Idempotent: a second sweep (another worker) finds nothing to do.
    releases = inventory.calls.count(("release", old["id"]))
    assert payment_service.expire_unpaid_orders(db, inventory, settings) == 0
    assert inventory.calls.count(("release", old["id"])) == releases

    # The customer can no longer pay it.
    assert client.post(f"{ORDERS_URL}/{old['order_number']}/pay", json={}, headers=shop.headers).status_code == 409


def test_expiry_sweep_marks_paid_when_webhook_was_missed(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    _age(db, order["order_number"], 45)
    assert payment_service.expire_unpaid_orders(db, inventory, get_settings()) == 0
    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status) == ("confirmed", "paid")
    assert ("release", order["id"]) not in inventory.calls


def test_expiry_sweep_skips_when_razorpay_unreachable(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    _age(db, order["order_number"], 45)
    razorpay_on.network_down = True
    assert payment_service.expire_unpaid_orders(db, inventory, get_settings()) == 0
    assert db_order(db, order["order_number"]).status == "pending"


def test_expiry_release_failure_keeps_order_pending(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    _age(db, order["order_number"], 45)

    def broken_release(order_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("inventory down")

    inventory.release = broken_release  # type: ignore[method-assign]
    assert payment_service.expire_unpaid_orders(db, inventory, get_settings()) == 0
    assert db_order(db, order["order_number"]).status == "pending"


def test_late_payment_after_expiry_is_refunded(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, razorpay_on: FakeRazorpay
) -> None:
    order = place_razorpay(client, shop)
    _age(db, order["order_number"], 45)
    assert payment_service.expire_unpaid_orders(db, inventory, get_settings()) == 1

    payment_id = razorpay_on.add_payment(order["payment"]["razorpay_order_id"])
    assert webhook(client, "payment.captured", razorpay_on.payments[payment_id]).status_code == 200
    stored = db_order(db, order["order_number"])
    assert (stored.status, stored.payment_status) == ("cancelled", "refunded")
    assert razorpay_on.refunds[0]["payment_id"] == payment_id
    assert "refunded" in stored.status_history[-1].note


def test_expiry_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scripts import expire_unpaid_orders as script

    calls: list[int] = []
    monkeypatch.setattr(script.payment_service, "expire_unpaid_orders", lambda *a, **k: calls.append(k["limit"]) or 0)
    assert script.main(["--limit", "5"]) == 0
    assert calls == [5]

