"""Permissions: internal token, CSRF header for cookie auth, auth-required endpoints."""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import CSRF_HEADERS, INTERNAL_HEADERS
from tests.factories import DEFAULT_PASSWORD, auth_headers, create_category, create_product, create_user, create_variant

ADDRESS = {
    "full_name": "Asha Kulkarni",
    "phone": "9876543210",
    "line1": "12, MG Road",
    "city": "Bengaluru",
    "state": "Karnataka",
    "pincode": "560001",
}


def internal_url() -> str:
    return f"/api/v1/internal/orders/{uuid.uuid4()}/status"


def test_internal_endpoint_requires_token(client: TestClient) -> None:
    body = {"status": "processing", "changed_by": "admin:priya"}
    missing = client.post(internal_url(), json=body)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "UNAUTHORIZED"

    wrong = client.post(internal_url(), json=body, headers={"X-Internal-Token": "nope-nope-nope-nope"})
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "FORBIDDEN"

    right = client.post(internal_url(), json=body, headers=INTERNAL_HEADERS)
    assert right.status_code == 404  # authenticated, but the order does not exist


def test_internal_changed_by_format_validated(client: TestClient) -> None:
    response = client.post(internal_url(), json={"status": "processing", "changed_by": "hacker"}, headers=INTERNAL_HEADERS)
    assert response.status_code == 422


def test_cookie_authenticated_mutation_requires_csrf_header(raw_client: TestClient, db: Session) -> None:
    create_user(db, email="csrf@testmail.in")
    login = raw_client.post("/api/v1/auth/login", json={"email": "csrf@testmail.in", "password": DEFAULT_PASSWORD})
    assert login.status_code == 200  # no cookies yet, so no header needed
    assert raw_client.cookies.get("pr_access")

    blocked = raw_client.post("/api/v1/addresses", json=ADDRESS)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "FORBIDDEN"

    allowed = raw_client.post("/api/v1/addresses", json=ADDRESS, headers=CSRF_HEADERS)
    assert allowed.status_code == 201, allowed.text

    # Safe methods never need the header.
    assert raw_client.get("/api/v1/addresses").status_code == 200


def test_bearer_requests_skip_csrf(raw_client: TestClient, db: Session) -> None:
    user = create_user(db)
    raw_client.cookies.set("pr_cart", "x" * 43)
    response = raw_client.post("/api/v1/addresses", json=ADDRESS, headers=auth_headers(user))
    assert response.status_code == 201


def test_guest_cart_cookie_also_requires_csrf_header(raw_client: TestClient, db: Session) -> None:
    category = create_category(db)
    product = create_product(db, category)
    variant = create_variant(db, product)
    body = {"variant_id": str(variant.id), "quantity": 1}
    first = raw_client.post("/api/v1/cart/items", json=body)
    assert first.status_code == 200  # no cookie yet
    assert raw_client.cookies.get("pr_cart")
    assert raw_client.post("/api/v1/cart/items", json=body).status_code == 403
    assert raw_client.post("/api/v1/cart/items", json=body, headers=CSRF_HEADERS).status_code == 200


def test_wishlist_requires_auth(client: TestClient, db: Session) -> None:
    assert client.get("/api/v1/wishlist").status_code == 401
    assert client.post("/api/v1/wishlist", json={"product_id": str(uuid.uuid4())}).status_code == 401
    assert client.delete(f"/api/v1/wishlist/{uuid.uuid4()}").status_code == 401


def test_wishlist_flow(client: TestClient, db: Session) -> None:
    user = create_user(db)
    headers = auth_headers(user)
    category = create_category(db)
    product = create_product(db, category, slug="honey", name="Wild Forest Honey")
    create_variant(db, product)

    added = client.post("/api/v1/wishlist", json={"product_id": str(product.id)}, headers=headers)
    assert added.status_code == 200
    assert [card["slug"] for card in added.json()["data"]] == ["honey"]
    again = client.post("/api/v1/wishlist", json={"product_id": str(product.id)}, headers=headers)
    assert len(again.json()["data"]) == 1

    missing = client.post("/api/v1/wishlist", json={"product_id": str(uuid.uuid4())}, headers=headers)
    assert missing.status_code == 404

    removed = client.delete(f"/api/v1/wishlist/{product.id}", headers=headers)
    assert removed.json()["data"] == []


def test_addresses_are_private(client: TestClient, db: Session) -> None:
    owner = auth_headers(create_user(db, email="owner@testmail.in"))
    other = auth_headers(create_user(db, email="other@testmail.in"))
    address_id = client.post("/api/v1/addresses", json=ADDRESS, headers=owner).json()["data"]["id"]
    assert client.patch(f"/api/v1/addresses/{address_id}", json={"city": "Mysuru"}, headers=other).status_code == 404
    assert client.delete(f"/api/v1/addresses/{address_id}", headers=other).status_code == 404
    assert client.get("/api/v1/addresses", headers=other).json()["data"] == []


def test_orders_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/orders").status_code == 401


def test_health_and_docs(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["data"] == {"status": "ok", "database": "ok"}
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/docs").status_code == 200


def test_unknown_route_uses_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
