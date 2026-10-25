"""Validation rules: Indian phone, PIN code, state/UT and the error envelope."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.constants import INDIAN_STATES
from app.schemas.common import normalize_phone, normalize_state, validate_password_strength, validate_pincode
from tests.factories import auth_headers, create_user


@pytest.mark.parametrize(
    "raw", ["9876543210", "+91 98765 43210", "+91-98765-43210", "919876543210", "09876543210", " 98765 43210 "]
)
def test_valid_phone_formats_normalise(raw: str) -> None:
    assert normalize_phone(raw) == "9876543210"


@pytest.mark.parametrize("raw", ["5876543210", "98765", "98765432101", "abcdefghij", ""])
def test_invalid_phones(raw: str) -> None:
    with pytest.raises(ValueError, match="10-digit Indian mobile"):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", ["560001", "110 001", "999999"])
def test_valid_pincodes(raw: str) -> None:
    assert len(validate_pincode(raw)) == 6


@pytest.mark.parametrize("raw", ["060001", "56001", "5600011", "56A001"])
def test_invalid_pincodes(raw: str) -> None:
    with pytest.raises(ValueError, match="PIN code"):
        validate_pincode(raw)


def test_states_are_case_insensitive_and_canonical() -> None:
    assert normalize_state("karnataka") == "Karnataka"
    assert normalize_state("  tamil   nadu ") == "Tamil Nadu"
    assert normalize_state("Delhi") == "Delhi"
    with pytest.raises(ValueError):
        normalize_state("Atlantis")


def test_states_list_has_28_states_and_8_uts() -> None:
    assert len(INDIAN_STATES) == 36
    assert len(set(INDIAN_STATES)) == 36


@pytest.mark.parametrize("password", ["short1", "abcdefgh", "12345678"])
def test_weak_passwords(password: str) -> None:
    with pytest.raises(ValueError):
        validate_password_strength(password)


def test_address_api_validation_envelope(client: TestClient, db: Session) -> None:
    headers = auth_headers(create_user(db))
    response = client.post(
        "/api/v1/addresses",
        json={
            "full_name": "Asha",
            "phone": "1234567890",
            "line1": "12, MG Road",
            "city": "Bengaluru",
            "state": "Wakanda",
            "pincode": "012345",
        },
        headers=headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Please check the highlighted fields and try again."
    fields = {d["field"]: d["message"] for d in body["error"]["details"]}
    assert fields == {
        "phone": "Enter a valid 10-digit Indian mobile number.",
        "state": "Select a valid Indian state or union territory.",
        "pincode": "Enter a valid 6-digit PIN code.",
    }


def test_address_normalises_valid_input(client: TestClient, db: Session) -> None:
    headers = auth_headers(create_user(db))
    response = client.post(
        "/api/v1/addresses",
        json={
            "full_name": "Asha Kulkarni",
            "phone": "+91 98765 43210",
            "line1": "12, MG Road",
            "city": "Bengaluru",
            "state": "karnataka",
            "pincode": "560 001",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["phone"], data["state"], data["pincode"], data["is_default"]) == (
        "9876543210",
        "Karnataka",
        "560001",
        True,
    )


def test_invalid_json_body(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_checkout_options(client: TestClient) -> None:
    data = client.get("/api/v1/checkout/options").json()["data"]
    assert data["free_shipping_threshold"] == 999.0
    assert {m["code"]: m["fee"] for m in data["delivery_methods"]} == {"standard": 79.0, "express": 149.0}
    cod = next(m for m in data["payment_methods"] if m["code"] == "cod")
    assert cod["enabled"] is True
    assert all(not m["enabled"] for m in data["payment_methods"] if m["code"] != "cod")
    assert "Karnataka" in data["states"] and len(data["states"]) == 36
