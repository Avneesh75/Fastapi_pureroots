"""HTTP client for Django's internal inventory API (ARCHITECTURE §6).

FastAPI never writes stock itself; it asks Django to reserve / commit / release / return.
"""
import logging
import uuid
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.core.constants import INTERNAL_TOKEN_HEADER
from app.core.exceptions import ConflictError, InsufficientStockError, ServiceUnavailableError

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = "We couldn't reach our inventory system. Please try again in a moment."
INSUFFICIENT_MESSAGE = "Some items in your cart are no longer available in the requested quantity."


class InventoryGateway(Protocol):
    """What the order service needs from inventory (the real client or a test fake)."""

    def reserve(self, order_id: uuid.UUID, order_number: str, items: list[dict[str, Any]]) -> dict[str, Any]: ...

    def commit(self, order_id: uuid.UUID) -> dict[str, Any]: ...

    def release(self, order_id: uuid.UUID) -> dict[str, Any]: ...

    def return_stock(self, order_id: uuid.UUID) -> dict[str, Any]: ...

    def availability(self, variant_ids: list[uuid.UUID]) -> dict[str, int]: ...


class InventoryClient:
    """Synchronous httpx client authenticated with ``X-Internal-Token``."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "InventoryClient":
        return cls(
            base_url=settings.django_internal_url,
            token=settings.internal_api_token,
            timeout=settings.internal_http_timeout_seconds,
        )

    # ---- public API ----

    def reserve(self, order_id: uuid.UUID, order_number: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"order_id": str(order_id), "order_number": order_number, "items": items}
        return self._request("POST", "reservations/", json=payload)

    def commit(self, order_id: uuid.UUID) -> dict[str, Any]:
        return self._request("POST", f"reservations/{order_id}/commit/", json={})

    def release(self, order_id: uuid.UUID) -> dict[str, Any]:
        return self._request("POST", f"reservations/{order_id}/release/", json={})

    def return_stock(self, order_id: uuid.UUID) -> dict[str, Any]:
        return self._request("POST", f"reservations/{order_id}/return/", json={})

    def availability(self, variant_ids: list[uuid.UUID]) -> dict[str, int]:
        params = {"variant_ids": ",".join(str(variant_id) for variant_id in variant_ids)}
        data = self._request("GET", "availability/", params=params)
        return {str(key): int(value) for key, value in data.items()}

    # ---- transport ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/internal/api/inventory/{path}"
        headers = {INTERNAL_TOKEN_HEADER: self._token, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
                response = client.request(method, url, json=json, params=params)
        except httpx.TimeoutException as exc:
            logger.warning("Inventory API timeout: %s %s", method, path)
            raise ServiceUnavailableError(UNAVAILABLE_MESSAGE) from exc
        except httpx.HTTPError as exc:
            logger.warning("Inventory API network error: %s %s (%s)", method, path, type(exc).__name__)
            raise ServiceUnavailableError(UNAVAILABLE_MESSAGE) from exc

        body = _safe_json(response)
        if response.status_code == 409:
            # Django error body: {ok:false, error:{code, message, details}}
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            details = error.get("details") if isinstance(error.get("details"), list) else []
            if error.get("code", "INSUFFICIENT_STOCK") == "INSUFFICIENT_STOCK":
                raise InsufficientStockError(INSUFFICIENT_MESSAGE, details=details)
            # e.g. INVALID_RESERVATION_STATE on commit/release/return
            logger.error("Inventory API %s %s conflict: %s", method, path, error.get("code"))
            raise ConflictError(
                "Stock for this order is not in a state that allows this change.",
                details=[{"inventory_code": error.get("code"), "message": error.get("message", "")}, *details],
            )
        if response.status_code >= 400:
            logger.error("Inventory API %s %s returned HTTP %s", method, path, response.status_code)
            raise ServiceUnavailableError(UNAVAILABLE_MESSAGE)
        return body


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
