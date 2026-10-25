"""Maintenance mode: /api/v1 answers 503 MAINTENANCE while the Django switch is on."""
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import maintenance
from app.core.clock import utcnow
from app.models.catalog import SiteSettings
from tests.conftest import INTERNAL_HEADERS
from tests.factories import create_category, create_product, create_variant


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    maintenance.reset_cache()
    yield
    maintenance.reset_cache()  # never leak an "on" state into other tests


def _set_maintenance(db: Session, enabled: bool, **extra) -> SiteSettings:
    row = db.query(SiteSettings).first() or SiteSettings(brand_name="PureRoots")
    row.maintenance_enabled = enabled
    for key, value in extra.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    maintenance.reset_cache()
    return row


def test_off_by_default_and_without_a_settings_row(client: TestClient, db: Session) -> None:
    create_variant(db, create_product(db, create_category(db)))
    db.commit()
    assert client.get("/api/v1/products").status_code == 200
    _set_maintenance(db, False)
    assert client.get("/api/v1/products").status_code == 200


def test_blocks_products_with_503_maintenance(client: TestClient, db: Session) -> None:
    _set_maintenance(db, True, maintenance_eta=utcnow() + timedelta(hours=2))
    response = client.get("/api/v1/products")
    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "MAINTENANCE"
    assert "maintenance" in body["error"]["message"].lower()
    assert 3600 < int(response.headers["Retry-After"]) <= 7200
    assert response.headers["Cache-Control"] == "no-store"

    for method, path in [("get", "/api/v1/cart"), ("post", "/api/v1/orders"), ("get", "/api/v1/categories")]:
        blocked = getattr(client, method)(path)
        assert blocked.status_code == 503, path
        assert blocked.json()["error"]["code"] == "MAINTENANCE"


def test_default_retry_after_without_eta(client: TestClient, db: Session) -> None:
    _set_maintenance(db, True)
    response = client.get("/api/v1/products")
    assert response.headers["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)


def test_health_internal_and_razorpay_stay_allowed(client: TestClient, db: Session) -> None:
    _set_maintenance(db, True)
    assert client.get("/api/v1/health").status_code == 200

    internal = client.post(
        "/api/v1/internal/orders/00000000-0000-0000-0000-000000000000/status",
        json={"status": "packed", "changed_by": "admin:root"},
        headers=INTERNAL_HEADERS,
    )
    assert internal.status_code != 503
    assert internal.json()["error"]["code"] != "MAINTENANCE"

    razorpay = client.post("/api/v1/payments/razorpay/webhook", json={})
    assert not (razorpay.status_code == 503 and razorpay.json()["error"]["code"] == "MAINTENANCE")

    # Outside /api/v1 (docs) is untouched too.
    assert client.get("/api/openapi.json").status_code == 200


def test_turning_it_off_reopens_the_api(client: TestClient, db: Session) -> None:
    _set_maintenance(db, True)
    assert client.get("/api/v1/products").status_code == 503
    _set_maintenance(db, False)
    assert client.get("/api/v1/products").status_code == 200


def test_state_is_cached(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = maintenance._read_state

    def counting(session):
        calls.append(1)
        return real(session)

    monkeypatch.setattr(maintenance, "_read_state", counting)
    for _ in range(3):
        client.get("/api/v1/health")  # exempt: no lookup
        client.get("/api/v1/categories")
    assert len(calls) == 1


def test_fails_open_on_a_database_error(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                        caplog: pytest.LogCaptureFixture) -> None:
    def broken(session):
        raise OperationalError("SELECT maintenance_enabled", {}, Exception("no such column"))

    monkeypatch.setattr(maintenance, "_read_state", broken)
    with caplog.at_level("ERROR", logger="app.core.maintenance"):
        response = client.get("/api/v1/categories")
    assert response.status_code == 200
    assert "Maintenance check failed" in caplog.text


def test_is_exempt() -> None:
    assert maintenance.is_exempt("/api/docs")
    assert maintenance.is_exempt("/api/v1/health")
    assert maintenance.is_exempt("/api/v1/internal/orders/x/status")
    assert maintenance.is_exempt("/api/v1/payments/razorpay/verify")
    assert not maintenance.is_exempt("/api/v1/payments/cod")
    assert not maintenance.is_exempt("/api/v1/healthy-snacks")
    assert not maintenance.is_exempt("/api/v1/products")
