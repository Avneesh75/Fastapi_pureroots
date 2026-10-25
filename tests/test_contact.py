"""Contact form: validation, persistence, Google Sheets sync via the LeadSink."""
import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.contact import ContactMessage, NewsletterSubscriber
from tests.fakes import FakeLeadSink

CONTACT_URL = "/api/v1/contact"


def payload(**overrides: str) -> dict[str, str]:
    body = {
        "full_name": "Ananya Iyer",
        "email": "Ananya@TestMail.in",
        "mobile": "98400 23456",
        "subject": "Bulk order",
        "message": "We would like to order 20 kg of millet atta for our cafe.",
        "product_interest": "Millet Atta",
        "city": "Chennai",
    }
    body.update(overrides)
    return body


def stored_message(db: Session) -> ContactMessage:
    db.expire_all()
    return db.query(ContactMessage).one()


def test_contact_saved_and_synced_with_exact_row_order(
    client: TestClient, db: Session, lead_sink: FakeLeadSink
) -> None:
    response = client.post(CONTACT_URL, json=payload())
    assert response.status_code == 201, response.text
    assert response.json()["success"] is True

    message = stored_message(db)
    assert str(message.id) == response.json()["data"]["id"]
    assert message.email == "ananya@testmail.in"
    assert message.mobile == "9840023456"
    assert message.status == "new"
    assert message.sheets_sync_status == "synced"
    assert message.sheets_synced_at is not None
    assert message.sheets_error is None

    assert len(lead_sink.rows) == 1
    row = lead_sink.rows[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row[0])
    assert row[1:] == [
        "Ananya Iyer",
        "ananya@testmail.in",
        "9840023456",
        "Bulk order",
        "Millet Atta",
        "Chennai",
        "We would like to order 20 kg of millet atta for our cafe.",
        "New",
    ]


def test_contact_optional_fields(client: TestClient, db: Session, lead_sink: FakeLeadSink) -> None:
    body = payload()
    del body["product_interest"]
    del body["city"]
    assert client.post(CONTACT_URL, json=body).status_code == 201
    assert lead_sink.rows[0][5:7] == ["", ""]


def test_sink_failure_marks_failed_but_request_succeeds(
    client: TestClient, db: Session, lead_sink: FakeLeadSink
) -> None:
    lead_sink.fail = True
    response = client.post(CONTACT_URL, json=payload())
    assert response.status_code == 201
    message = stored_message(db)
    assert message.sheets_sync_status == "failed"
    assert "HTTP 403" in (message.sheets_error or "")


def test_disabled_sink_marks_disabled(client: TestClient, db: Session, lead_sink: FakeLeadSink) -> None:
    lead_sink.enabled = False
    response = client.post(CONTACT_URL, json=payload())
    assert response.status_code == 201
    assert stored_message(db).sheets_sync_status == "disabled"
    assert lead_sink.rows == []


def test_contact_validation_errors(client: TestClient, db: Session, lead_sink: FakeLeadSink) -> None:
    response = client.post(
        CONTACT_URL, json=payload(full_name="A", mobile="12345", message="Too short", email="not-an-email")
    )
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    fields = {detail["field"]: detail["message"] for detail in body["error"]["details"]}
    assert set(fields) == {"full_name", "mobile", "message", "email"}
    assert fields["mobile"] == "Enter a valid 10-digit Indian mobile number."
    assert db.query(ContactMessage).count() == 0
    assert lead_sink.rows == []


def test_message_length_limits(client: TestClient) -> None:
    assert client.post(CONTACT_URL, json=payload(message="x" * 3001)).status_code == 422
    assert client.post(CONTACT_URL, json=payload(message="x" * 10)).status_code == 201


def test_newsletter_subscribe_is_idempotent(client: TestClient, db: Session) -> None:
    first = client.post("/api/v1/newsletter/subscribe", json={"email": "Fan@TestMail.in", "source": "footer"})
    second = client.post("/api/v1/newsletter/subscribe", json={"email": "fan@testmail.in"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["data"] == {"email": "fan@testmail.in", "subscribed": True}
    db.expire_all()
    assert db.query(NewsletterSubscriber).count() == 1
