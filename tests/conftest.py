"""Shared fixtures: in-memory SQLite (StaticPool), fresh schema per test, fake gateways."""
import os

# Must be set before any ``app`` import (settings and the engine are created at import time).
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["ENVIRONMENT"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-that-is-definitely-long-enough"
os.environ["INTERNAL_API_TOKEN"] = "test-internal-token-123456"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["GOOGLE_SHEETS_ENABLED"] = "false"
os.environ["COOKIE_SECURE"] = "false"
os.environ["SMTP_HOST"] = ""
os.environ["FREE_SHIPPING_THRESHOLD"] = "999"
os.environ["STANDARD_SHIPPING_FEE"] = "79"
os.environ["EXPRESS_SHIPPING_FEE"] = "149"
os.environ["PUBLIC_MEDIA_BASE"] = "/media"
os.environ["PLACEHOLDER_BASE"] = "/placeholder"
os.environ["PUBLIC_SITE_URL"] = "http://localhost:8080"
os.environ["LOG_LEVEL"] = "WARNING"
# Razorpay is off by default (COD-only); tests/test_razorpay.py switches it on per test.
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ["RAZORPAY_WEBHOOK_SECRET"] = ""
os.environ["PAYMENT_EXPIRY_SWEEP_SECONDS"] = "0"
# Direct UPI is off by default too (a real UPI_VPA in the repo .env must not leak into tests);
# tests/test_upi.py switches it on per test.
os.environ["UPI_VPA"] = ""

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models as _models  # noqa: E402,F401
from app.core import security  # noqa: E402
from app.database import Base, CatalogBase, SessionLocal, engine  # noqa: E402
from app.dependencies import get_db, get_email_sender, get_inventory_client, get_lead_sink  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from tests.fakes import FakeEmailSender, FakeInventory, FakeLeadSink  # noqa: E402

security.BCRYPT_ROUNDS = 4  # fast hashing in tests


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:  # noqa: ANN001
    """Enforce FKs like PostgreSQL does, so insert-ordering bugs fail in tests."""
    dbapi_connection.execute("PRAGMA foreign_keys=ON")

CSRF_HEADERS = {"X-Requested-With": "PureRoot"}
INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token-123456"}


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(engine)
    CatalogBase.metadata.create_all(engine)
    yield
    CatalogBase.metadata.drop_all(engine)
    Base.metadata.drop_all(engine)


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def inventory() -> FakeInventory:
    return FakeInventory()


@pytest.fixture
def lead_sink() -> FakeLeadSink:
    return FakeLeadSink()


@pytest.fixture
def email_sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def test_app(
    db: Session, inventory: FakeInventory, lead_sink: FakeLeadSink, email_sender: FakeEmailSender
) -> Iterator[FastAPI]:
    fastapi_app.dependency_overrides[get_db] = lambda: db
    fastapi_app.dependency_overrides[get_inventory_client] = lambda: inventory
    fastapi_app.dependency_overrides[get_lead_sink] = lambda: lead_sink
    fastapi_app.dependency_overrides[get_email_sender] = lambda: email_sender
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def client(test_app: FastAPI) -> Iterator[TestClient]:
    """Behaves like the React islands: always sends ``X-Requested-With: PureRoot``."""
    with TestClient(test_app, headers=CSRF_HEADERS) as test_client:
        yield test_client


@pytest.fixture
def raw_client(test_app: FastAPI) -> Iterator[TestClient]:
    """A client that does NOT send the CSRF header (for CSRF tests)."""
    with TestClient(test_app) as test_client:
        yield test_client
