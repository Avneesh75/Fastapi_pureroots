"""GoogleSheetsLeadSink with a mocked AuthorizedSession, plus lead-sink factory and key handling."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.config import Settings, get_settings
from app.services.google_sheets_service import GoogleSheetsLeadSink
from app.services.lead_sink import LeadRecord, LeadSinkError, NullLeadSink, build_lead_sink

FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIfake\n-----END PRIVATE KEY-----\n"


def lead(**overrides: str) -> LeadRecord:
    values = {
        "full_name": "Rohan Mehta",
        "email": "rohan@testmail.in",
        "mobile": "9820034567",
        "subject": "Wholesale enquiry",
        "product_interest": "A2 Ghee",
        "city": "Mumbai",
        "message": "Do you supply to restaurants in Bandra?",
    }
    values.update(overrides)
    return LeadRecord(created_at=datetime(2026, 9, 27, 4, 30, 5, tzinfo=timezone.utc), **values)


def sink_with(session: Mock, worksheet: str = "Enquiries") -> GoogleSheetsLeadSink:
    return GoogleSheetsLeadSink(
        "sheet-123", worksheet, "leads@pureroot.iam.gserviceaccount.com", FAKE_KEY, session=session
    )


def ok_session() -> Mock:
    session = Mock()
    session.post.return_value = Mock(status_code=200, text='{"updates": {}}')
    return session


def test_push_calls_values_append_with_expected_request() -> None:
    session = ok_session()
    sink_with(session).push(lead())

    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args[0] == (
        "https://sheets.googleapis.com/v4/spreadsheets/sheet-123/values/%27Enquiries%27%21A%3AI:append"
    )
    assert kwargs["params"] == {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    assert kwargs["json"] == {
        "majorDimension": "ROWS",
        "values": [
            [
                "2026-09-27 10:00:05",  # IST
                "Rohan Mehta",
                "rohan@testmail.in",
                "9820034567",
                "Wholesale enquiry",
                "A2 Ghee",
                "Mumbai",
                "Do you supply to restaurants in Bandra?",
                "New",
            ]
        ],
    }
    assert kwargs["timeout"] == 10.0


def test_range_escapes_worksheet_names() -> None:
    sink = sink_with(ok_session(), worksheet="Rohan's Leads")
    assert sink.range_a1 == "'Rohan''s Leads'!A:I"
    assert sink.append_url.endswith("/values/%27Rohan%27%27s%20Leads%27%21A%3AI:append")


def test_http_error_raises_lead_sink_error() -> None:
    session = Mock()
    session.post.return_value = Mock(status_code=403, text="The caller does not have permission")
    with pytest.raises(LeadSinkError, match="HTTP 403"):
        sink_with(session).push(lead())


def test_network_error_raises_lead_sink_error() -> None:
    session = Mock()
    session.post.side_effect = ConnectionError("boom")
    with pytest.raises(LeadSinkError, match="ConnectionError"):
        sink_with(session).push(lead())


def test_formula_injection_is_neutralised() -> None:
    session = ok_session()
    sink_with(session).push(lead(full_name="=HYPERLINK(\"http://evil\")", message="+cmd"))
    row = session.post.call_args.kwargs["json"]["values"][0]
    assert row[1] == "'=HYPERLINK(\"http://evil\")"
    assert row[7] == "'+cmd"


def test_factory_returns_null_sink_when_disabled() -> None:
    settings = get_settings().model_copy(update={"google_sheets_enabled": False})
    assert isinstance(build_lead_sink(settings), NullLeadSink)


def test_factory_returns_null_sink_when_misconfigured() -> None:
    settings = get_settings().model_copy(update={"google_sheets_enabled": True, "google_sheets_spreadsheet_id": ""})
    sink = build_lead_sink(settings)
    assert isinstance(sink, NullLeadSink)
    assert sink.enabled is False


def test_factory_returns_sheets_sink_when_configured() -> None:
    settings = get_settings().model_copy(
        update={
            "google_sheets_enabled": True,
            "google_sheets_spreadsheet_id": "sheet-123",
            "google_service_account_email": "leads@pureroot.iam.gserviceaccount.com",
            "google_private_key": FAKE_KEY,
        }
    )
    sink = build_lead_sink(settings)
    assert isinstance(sink, GoogleSheetsLeadSink)
    assert sink.enabled is True
    assert sink.spreadsheet_id == "sheet-123"


def test_private_key_literal_newlines_are_converted() -> None:
    raw = '"-----BEGIN PRIVATE KEY-----\\nMIIfake\\n-----END PRIVATE KEY-----\\n"'
    settings = Settings(google_private_key=raw)  # type: ignore[call-arg]
    assert settings.google_private_key == FAKE_KEY
