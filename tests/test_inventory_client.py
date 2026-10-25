"""InventoryClient against a mocked Django internal API (httpx.MockTransport)."""
import json
import uuid

import httpx
import pytest

from app.core.exceptions import ConflictError, InsufficientStockError, ServiceUnavailableError
from app.services.inventory_client import InventoryClient

ORDER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def client_for(handler) -> InventoryClient:  # type: ignore[no-untyped-def]
    return InventoryClient("http://django:8000/", "secret-token-123456", transport=httpx.MockTransport(handler))


def test_reserve_sends_token_and_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers["X-Internal-Token"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "reservations": []})

    result = client_for(handler).reserve(ORDER_ID, "PR-260928-ABCDE", [{"variant_id": "v1", "quantity": 2}])
    assert result["ok"] is True
    assert seen["url"] == "http://django:8000/internal/api/inventory/reservations/"
    assert seen["token"] == "secret-token-123456"
    assert seen["body"] == {
        "order_id": str(ORDER_ID),
        "order_number": "PR-260928-ABCDE",
        "items": [{"variant_id": "v1", "quantity": 2}],
    }


def test_release_url() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "reservations": []})

    client_for(handler).release(ORDER_ID)
    assert urls == [f"http://django:8000/internal/api/inventory/reservations/{ORDER_ID}/release/"]


def test_409_insufficient_stock_raises_with_details() -> None:
    details = [{"variant_id": "v1", "sku": "PR-ATTA-001-1KG", "requested": 3, "available": 1}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "error": {"code": "INSUFFICIENT_STOCK", "message": "x", "details": details}})

    with pytest.raises(InsufficientStockError) as info:
        client_for(handler).reserve(ORDER_ID, "PR-1", [])
    assert info.value.details == details


def test_409_invalid_state_raises_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"ok": False, "error": {"code": "INVALID_RESERVATION_STATE", "message": "not active", "details": []}}
        )

    with pytest.raises(ConflictError):
        client_for(handler).commit(ORDER_ID)


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ConnectError("down")])
def test_network_errors_are_service_unavailable(error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(ServiceUnavailableError):
        client_for(handler).release(ORDER_ID)


def test_server_error_is_service_unavailable() -> None:
    with pytest.raises(ServiceUnavailableError):
        client_for(lambda request: httpx.Response(500, text="boom")).commit(ORDER_ID)
