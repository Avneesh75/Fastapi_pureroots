"""Test doubles for external gateways (Django inventory API, lead sink, email)."""
import uuid
from typing import Any

from app.services.email_service import OutgoingEmail
from app.services.lead_sink import LeadRecord, LeadSinkError


class FakeInventory:
    """Records every inventory call; can be told to fail ``reserve``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reserved_items: dict[str, list[dict[str, Any]]] = {}
        self.reserve_error: Exception | None = None

    def reserve(self, order_id: uuid.UUID, order_number: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(("reserve", str(order_id)))
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reserved_items[str(order_id)] = items
        return {"ok": True, "reservations": []}

    def commit(self, order_id: uuid.UUID) -> dict[str, Any]:
        self.calls.append(("commit", str(order_id)))
        return {"ok": True}

    def release(self, order_id: uuid.UUID) -> dict[str, Any]:
        self.calls.append(("release", str(order_id)))
        return {"ok": True}

    def return_stock(self, order_id: uuid.UUID) -> dict[str, Any]:
        self.calls.append(("return", str(order_id)))
        return {"ok": True}

    def availability(self, variant_ids: list[uuid.UUID]) -> dict[str, int]:
        return {}

    @property
    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]


class FakeLeadSink:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.fail = False
        self.rows: list[list[str]] = []

    def push(self, lead: LeadRecord) -> None:
        if self.fail:
            raise LeadSinkError("Google Sheets API returned HTTP 403: permission denied")
        self.rows.append(lead.to_row())


class FakeEmailSender:
    def __init__(self) -> None:
        self.messages: list[OutgoingEmail] = []

    def send(self, message: OutgoingEmail) -> None:
        self.messages.append(message)
