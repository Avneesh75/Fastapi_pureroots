"""Direct UPI: options, checkout payload + deep link, UTR submission, staff verification, guards and expiry."""
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.clock import utcnow
from app.models.order import Order, Payment
from app.services import upi_service
from app.services.payments.upi import UTR_MESSAGE, build_upi_uri, format_amount, is_valid_utr, is_valid_vpa
from tests.conftest import INTERNAL_HEADERS
from tests.factories import checkout_payload
from tests.fakes import FakeInventory
from tests.test_razorpay import (  # noqa: F401 - fixtures
    ORDERS_URL,
    OPTIONS_URL,
    FakeRazorpay,
    Shop,
    db_order,
    fill_cart,
    place_razorpay,
    razorpay_api,
    razorpay_on,
    set_status,
    shop,
)

VPA = "pureroots@upi"
GUEST_EMAIL = "guest.buyer@testmail.in"


@pytest.fixture
def upi_on() -> Iterator[None]:
    previous = os.environ.get("UPI_VPA")
    os.environ["UPI_VPA"] = VPA
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("UPI_VPA", None)
        else:
            os.environ["UPI_VPA"] = previous
        get_settings.cache_clear()


def place_upi(client: TestClient, shop: Shop, *, quantity: int = 1, guest: bool = False) -> dict[str, Any]:
    headers: dict[str, str] = {} if guest else shop.headers
    fill_cart(client, shop, quantity, headers)
    body = checkout_payload(payment_method="upi")
    if guest:
        body["customer"]["email"] = GUEST_EMAIL
    response = client.post(ORDERS_URL, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def submit(client: TestClient, order_number: str, utr: str = "123456789012", *, headers: dict[str, str] | None = None, **extra: Any):  # type: ignore[no-untyped-def]
    return client.post(f"{ORDERS_URL}/{order_number}/upi-submit", json={"utr": utr, **extra}, headers=headers or {})


def verify(client: TestClient, order_id: str, decision: str, note: str = "", changed_by: str = "admin:priya"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/internal/orders/{order_id}/upi-verify",
        json={"decision": decision, "note": note, "changed_by": changed_by},
        headers=INTERNAL_HEADERS,
    )


def upi_rows(db: Session, order_number: str) -> list[Payment]:
    return [row for row in db_order(db, order_number).payments if row.provider == "upi"]


def notes(order: dict[str, Any]) -> list[str]:
    return [entry["note"] for entry in order["status_history"]]


# --------------------------------------------------------------------------- helpers


def test_deep_link_is_encoded_per_upi_spec() -> None:
    uri = build_upi_uri("pureroots@okicici", "PureRoots Aahar", Decimal("878.5"), "PR-260930-ABCDE")
    assert uri == (
        "upi://pay?pa=pureroots@okicici&pn=PureRoots%20Aahar&am=878.50&cu=INR"
        "&tn=PureRoots%20order%20PR-260930-ABCDE&tr=PR-260930-ABCDE"
    )
    tricky = build_upi_uri("shop.name-1@ybl", "Aahar & Sons", "1199.995", "PR-1")
    assert "pn=Aahar%20%26%20Sons" in tricky and "am=1200.00" in tricky and "+" not in tricky
    assert format_amount(10) == "10.00"


def test_vpa_and_utr_validation() -> None:
    assert is_valid_vpa("pureroots@okicici") and is_valid_vpa("a.b-c_d@upi")
    for bad in ("", "pureroots", "p@1bank", "@upi", "x@upi", "pure roots@upi"):
        assert not is_valid_vpa(bad), bad
    assert is_valid_utr("123456789012") and is_valid_utr("1234 5678 9012")
    for bad in ("12345678901", "1234567890123", "12345678901a", ""):
        assert not is_valid_utr(bad), bad


# --------------------------------------------------------------------------- options


def test_options_without_upi_vpa(client: TestClient) -> None:
    codes = [m["code"] for m in client.get(OPTIONS_URL).json()["data"]["payment_methods"]]
    assert codes == ["cod"]


def test_options_with_invalid_upi_vpa_hide_upi(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPI_VPA", "not-a-upi-id")
    get_settings.cache_clear()
    try:
        codes = [m["code"] for m in client.get(OPTIONS_URL).json()["data"]["payment_methods"]]
    finally:
        monkeypatch.delenv("UPI_VPA")
        get_settings.cache_clear()
    assert codes == ["cod"]


def test_options_with_upi_vpa_list_upi_after_cod(client: TestClient, upi_on: None) -> None:
    methods = client.get(OPTIONS_URL).json()["data"]["payment_methods"]
    assert [m["code"] for m in methods] == ["cod", "upi"]
    upi = methods[1]
    assert upi == {
        "code": "upi",
        "label": "UPI",
        "description": "Pay instantly with GPay, PhonePe, Paytm, BHIM or any UPI app",
        "enabled": True,
    }


def test_options_with_upi_and_razorpay(client: TestClient, upi_on: None, razorpay_on: FakeRazorpay) -> None:
    methods = client.get(OPTIONS_URL).json()["data"]["payment_methods"]
    assert [m["code"] for m in methods] == ["cod", "upi", "razorpay"]
    assert methods[2]["label"] == "Cards, netbanking & wallets (Razorpay)"
    assert "UPI" not in methods[2]["label"]  # never confused with the direct UPI option


# --------------------------------------------------------------------------- checkout


def test_upi_checkout_payload(client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, upi_on: None) -> None:
    order = place_upi(client, shop, quantity=2)
    assert order["status"] == "pending" and order["payment_status"] == "pending"
    assert order["payment_method"] == "upi" and order["can_pay"] is False
    payment = order["payment"]
    number = order["order_number"]
    total = order["grand_total"]
    assert payment["provider"] == "upi" and payment["vpa"] == VPA and payment["payee_name"] == "PureRoots Aahar"
    assert payment["amount"] == total and payment["currency"] == "INR" and payment["order_number"] == number
    assert payment["note"] == f"PureRoots order {number}"
    assert payment["upi_uri"] == (
        f"upi://pay?pa={VPA}&pn=PureRoots%20Aahar&am={total:.2f}&cu=INR&tn=PureRoots%20order%20{number}&tr={number}"
    )
    assert payment["expires_at"]  # placed_at + UPI_PAYMENT_EXPIRY_MINUTES
    assert order["upi"]["state"] == "awaiting_payment" and order["upi"]["can_submit"] is True
    assert order["upi"]["utr"] is None
    assert inventory.actions == ["reserve"]
    assert client.get("/api/v1/cart", headers=shop.headers).json()["data"]["items"] == []
    rows = upi_rows(db, number)
    assert len(rows) == 1 and rows[0].status == "awaiting_payment" and rows[0].provider_reference == ""
    assert Decimal(str(rows[0].amount)) == Decimal(str(total))
    # A later GET carries the UPI block (not the checkout-only `payment`).
    detail = client.get(f"{ORDERS_URL}/{number}", headers=shop.headers).json()["data"]
    assert detail["payment"] is None and detail["upi"]["upi_uri"] == payment["upi_uri"]
    assert detail["payment_status_label"] == "Payment pending"


def test_upi_rejected_at_checkout_when_not_configured(client: TestClient, shop: Shop) -> None:
    fill_cart(client, shop)
    response = client.post(ORDERS_URL, json=checkout_payload(payment_method="upi"), headers=shop.headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "payment_method"


# --------------------------------------------------------------------------- upi-submit


def test_submit_utr(client: TestClient, db: Session, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    response = submit(client, order["order_number"], "1234 5678 9012", headers=shop.headers, payer_vpa="Asha@OKBank")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "pending" and data["payment_status"] == "awaiting_verification"
    assert data["payment_status_label"] == "Verifying payment"
    assert data["upi"]["state"] == "awaiting_verification" and data["upi"]["utr"] == "123456789012"
    assert data["upi"]["payer_vpa"] == "asha@okbank" and data["upi"]["submitted_at"]
    assert "UPI payment submitted (UTR 123456789012)" in notes(data)
    row = upi_rows(db, order["order_number"])[0]
    assert row.status == "submitted" and row.provider_reference == "123456789012"
    assert row.raw_response["submitted_at"]

    again = submit(client, order["order_number"], headers=shop.headers)  # same UTR again: no-op
    assert again.status_code == 200 and len(notes(again.json()["data"])) == len(notes(data))

    fixed = submit(client, order["order_number"], "999988887777", headers=shop.headers)  # correction while pending
    assert fixed.status_code == 200 and fixed.json()["data"]["upi"]["utr"] == "999988887777"
    assert len(upi_rows(db, order["order_number"])) == 1


@pytest.mark.parametrize("utr", ["12345", "12345678901a", "1234567890123", ""])
def test_submit_invalid_utr(client: TestClient, shop: Shop, upi_on: None, utr: str) -> None:
    order = place_upi(client, shop)
    response = submit(client, order["order_number"], utr, headers=shop.headers)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == UTR_MESSAGE and error["details"][0]["field"] == "utr"


def test_submit_invalid_payer_vpa(client: TestClient, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    response = submit(client, order["order_number"], headers=shop.headers, payer_vpa="asha")
    assert response.status_code == 422 and response.json()["error"]["details"][0]["field"] == "payer_vpa"


def test_submit_duplicate_utr_across_orders(client: TestClient, shop: Shop, upi_on: None) -> None:
    first = place_upi(client, shop)
    second = place_upi(client, shop)
    assert submit(client, first["order_number"], headers=shop.headers).status_code == 200
    response = submit(client, second["order_number"], headers=shop.headers)
    assert response.status_code == 409
    assert "already been used" in response.json()["error"]["message"]
    assert client.get(f"{ORDERS_URL}/{second['order_number']}", headers=shop.headers).json()["data"]["payment_status"] == "pending"


def test_submit_as_guest_with_email(client: TestClient, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop, guest=True)
    number = order["order_number"]
    assert submit(client, number).status_code == 404  # no email, not signed in
    assert submit(client, number, email="someone.else@testmail.in").status_code == 404  # wrong email
    assert submit(client, number, headers=shop.headers).status_code == 404  # signed in, but not the owner
    response = submit(client, number, email=GUEST_EMAIL.upper())
    assert response.status_code == 200, response.text
    assert response.json()["data"]["payment_status"] == "awaiting_verification"


def test_submit_on_non_upi_order_is_409(client: TestClient, shop: Shop, upi_on: None) -> None:
    fill_cart(client, shop)
    cod = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers).json()["data"]
    response = submit(client, cod["order_number"], headers=shop.headers)
    assert response.status_code == 409


# --------------------------------------------------------------------------- internal verify


def test_verify_approve(client: TestClient, db: Session, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    response = verify(client, order["id"], "approve", "Seen in HDFC app")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "confirmed" and data["payment_status"] == "paid"
    assert data["upi"]["state"] == "paid" and data["upi"]["can_submit"] is False
    assert "UPI payment confirmed by admin:priya: Seen in HDFC app" in notes(data)
    confirm_entry = [h for h in data["status_history"] if h["to_status"] == "confirmed"][0]
    assert confirm_entry["from_status"] == "pending" and confirm_entry["changed_by"] == "admin:priya"
    assert upi_rows(db, order["order_number"])[0].status == "paid"

    again = verify(client, order["id"], "approve")  # idempotent
    assert again.status_code == 200 and len(again.json()["data"]["status_history"]) == len(data["status_history"])
    assert verify(client, order["id"], "reject", "late").status_code == 409  # can't reject a confirmed payment
    assert submit(client, order["order_number"], "111122223333", headers=shop.headers).status_code == 409


def test_verify_reject_then_resubmit(client: TestClient, db: Session, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    response = verify(client, order["id"], "reject", "No credit for this UTR")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "pending" and data["payment_status"] == "failed"
    assert data["upi"]["state"] == "rejected" and data["upi"]["can_submit"] is True
    assert data["upi"]["rejection_reason"] == "No credit for this UTR"
    assert any("rejected by admin:priya: No credit for this UTR" in note for note in notes(data))

    again = verify(client, order["id"], "reject", "twice")  # idempotent
    assert again.status_code == 200 and len(again.json()["data"]["status_history"]) == len(data["status_history"])
    assert verify(client, order["id"], "approve").status_code == 409  # nothing awaiting verification now

    resubmitted = submit(client, order["order_number"], "555566667777", headers=shop.headers)
    assert resubmitted.status_code == 200
    assert resubmitted.json()["data"]["payment_status"] == "awaiting_verification"
    statuses = [row.status for row in upi_rows(db, order["order_number"])]
    assert statuses == ["rejected", "submitted"]  # the rejected attempt is kept
    assert verify(client, order["id"], "approve").json()["data"]["status"] == "confirmed"


def test_rejected_utr_can_be_resubmitted_on_same_order_only(client: TestClient, shop: Shop, upi_on: None) -> None:
    first = place_upi(client, shop)
    other = place_upi(client, shop)
    submit(client, first["order_number"], headers=shop.headers)
    verify(client, first["id"], "reject", "Not received yet")
    assert submit(client, other["order_number"], headers=shop.headers).status_code == 409
    assert submit(client, first["order_number"], headers=shop.headers).status_code == 200


def test_verify_conflicts(client: TestClient, shop: Shop, upi_on: None) -> None:
    fill_cart(client, shop)
    cod = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers).json()["data"]
    response = verify(client, cod["id"], "approve")
    assert response.status_code == 409 and "isn't paid by UPI" in response.json()["error"]["message"]
    unpaid = place_upi(client, shop)  # no UTR yet
    assert verify(client, unpaid["id"], "approve").status_code == 409
    assert verify(client, unpaid["id"], "reject").status_code == 409
    bad = client.post(
        f"/api/v1/internal/orders/{unpaid['id']}/upi-verify",
        json={"decision": "maybe", "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )
    assert bad.status_code == 422
    no_token = client.post(f"/api/v1/internal/orders/{unpaid['id']}/upi-verify", json={"decision": "approve"})
    assert no_token.status_code in (401, 403)


# --------------------------------------------------------------------------- guards, cancellation


def test_staff_cannot_confirm_unpaid_upi_order(client: TestClient, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    assert set_status(client, order["id"], "confirmed").status_code == 409
    submit(client, order["order_number"], headers=shop.headers)
    response = set_status(client, order["id"], "confirmed")
    assert response.status_code == 409 and "Confirm payment received" in response.json()["error"]["message"]


def test_cancel_paid_upi_order_notes_refund_due(client: TestClient, shop: Shop, inventory: FakeInventory, upi_on: None) -> None:
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    verify(client, order["id"], "approve")
    response = set_status(client, order["id"], "cancelled")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    total = f"₹{Decimal(str(data['grand_total'])):,.2f}"
    assert data["status"] == "cancelled" and data["payment_status"] == "paid"  # no automatic refund
    assert any(f"Refund due: send {total} back to the customer's UPI" in note for note in notes(data))
    assert "release" in inventory.actions
    refunded = set_status(client, order["id"], "refunded")
    assert refunded.status_code == 200 and refunded.json()["data"]["payment_status"] == "refunded"
    assert refunded.json()["data"]["upi"]["state"] == "refunded"


def test_customer_cancel_while_verifying_then_admin_approves(client: TestClient, shop: Shop, upi_on: None) -> None:
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    cancelled = client.post(f"{ORDERS_URL}/{order['order_number']}/cancel", headers=shop.headers)
    assert cancelled.status_code == 200
    assert any("awaiting verification" in note for note in notes(cancelled.json()["data"]))
    approved = verify(client, order["id"], "approve")
    assert approved.status_code == 200
    data = approved.json()["data"]
    assert data["status"] == "cancelled" and data["payment_status"] == "paid"
    assert any("Refund due" in note for note in notes(data))


# --------------------------------------------------------------------------- expiry


def test_expiry_cancels_upi_orders_without_utr(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, upi_on: None
) -> None:
    settings = get_settings()
    stale = place_upi(client, shop)
    fresh = place_upi(client, shop)
    now = utcnow() + timedelta(minutes=settings.upi_payment_expiry_minutes + 1)
    # `fresh` was placed "now" relative to the sweep, so move only `stale` back in time.
    db.query(Order).filter(Order.id == uuid.UUID(fresh["id"])).update({"placed_at": now - timedelta(minutes=5)})
    db.commit()

    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=now) == 1
    cancelled = db_order(db, stale["order_number"])
    assert cancelled.status == "cancelled"
    assert cancelled.status_history[-1].note == "UPI payment not received in time"
    assert [row.status for row in cancelled.payments] == ["expired"]
    assert ("release", stale["id"]) in inventory.calls
    assert db_order(db, fresh["order_number"]).status == "pending"
    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=now) == 0  # idempotent


def test_expiry_never_cancels_orders_awaiting_verification(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, upi_on: None
) -> None:
    settings = get_settings()
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    later = utcnow() + timedelta(days=3)
    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=later) == 0
    assert db_order(db, order["order_number"]).status == "pending"
    assert "release" not in inventory.actions


def test_expiry_after_rejection_restarts_the_window(
    client: TestClient, db: Session, shop: Shop, inventory: FakeInventory, upi_on: None
) -> None:
    settings = get_settings()
    window = timedelta(minutes=settings.upi_payment_expiry_minutes)
    order = place_upi(client, shop)
    db.query(Order).filter(Order.id == uuid.UUID(order["id"])).update({"placed_at": utcnow() - window - timedelta(minutes=30)})
    db.commit()
    submit(client, order["order_number"], headers=shop.headers)
    verify(client, order["id"], "reject", "Not found")
    assert upi_service.expire_unpaid_orders(db, inventory, settings) == 0  # just rejected: time to resubmit
    assert upi_service.expire_unpaid_orders(db, inventory, settings, now=utcnow() + window + timedelta(minutes=1)) == 1
    assert db_order(db, order["order_number"]).status == "cancelled"


def test_run_sweep_once_includes_upi(upi_on: None) -> None:
    from app.services import payment_expiry

    settings = get_settings()
    assert payment_expiry.sweep_enabled(settings.model_copy(update={"payment_expiry_sweep_seconds": 60}))
    assert not payment_expiry.sweep_enabled(settings)  # 0 in tests


# --------------------------------------------------------------------------- other providers unaffected


def test_cod_unaffected_by_upi(client: TestClient, shop: Shop, upi_on: None) -> None:
    fill_cart(client, shop)
    response = client.post(ORDERS_URL, json=checkout_payload(), headers=shop.headers)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "confirmed" and data["payment_status"] == "cod_pending"
    assert data["payment"] is None and data["upi"] is None
    assert data["payment_status_label"] == "Pay on delivery"


def test_razorpay_unaffected_by_upi(client: TestClient, shop: Shop, upi_on: None, razorpay_on: FakeRazorpay) -> None:
    order = place_razorpay(client, shop)
    assert order["payment"]["provider"] == "razorpay" and order["payment"]["key_id"]
    assert order["upi"] is None and order["can_pay"] is True
    assert submit(client, order["order_number"], headers=shop.headers).status_code == 409
    assert set_status(client, order["id"], "confirmed").status_code == 409  # Razorpay guard still applies


def test_internal_endpoints_accept_a_null_note(client: TestClient, shop: Shop, upi_on: None) -> None:
    """Django's client sends ``"note": null`` for a blank note."""
    order = place_upi(client, shop)
    submit(client, order["order_number"], headers=shop.headers)
    response = client.post(
        f"/api/v1/internal/orders/{order['id']}/upi-verify",
        json={"decision": "approve", "note": None, "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200, response.text
    moved = client.post(
        f"/api/v1/internal/orders/{order['id']}/status",
        json={"status": "processing", "note": None, "changed_by": "admin:priya"},
        headers=INTERNAL_HEADERS,
    )
    assert moved.status_code == 200, moved.text
