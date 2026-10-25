"""Razorpay provider (https://razorpay.com/docs/api/). Plain httpx + HTTP basic auth, no SDK.

Enabled only when ``RAZORPAY_KEY_ID`` and ``RAZORPAY_KEY_SECRET`` are set. The key secret and the
webhook secret are never logged or returned; only the public key id reaches the browser.
Amounts sent to Razorpay are integers in paise, always computed from the server-side
``orders.grand_total``.
"""
import hashlib
import hmac
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.services.payments.base import PaymentInitiation

if TYPE_CHECKING:
    from app.models.order import Order

logger = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 10.0
CURRENCY = "INR"
CHECKOUT_NAME = "PureRoots Aahar"

# Tests replace this with ``httpx.MockTransport``; ``None`` means real network I/O.
HTTP_TRANSPORT: httpx.BaseTransport | None = None

UNAVAILABLE_MESSAGE = "We couldn't reach our payment partner. Please try again in a moment."
TIMEOUT_MESSAGE = "Our payment partner is taking too long to respond. Please try again in a moment."
CHECKOUT_UNAVAILABLE_MESSAGE = (
    "We couldn't start the online payment, so your order was not placed and nothing was charged. "
    "Please try again in a moment, or choose Cash on Delivery."
)
REJECTED_MESSAGE = "Our payment partner could not process this request. Please try again shortly."

# Fields of a Razorpay payment/order/refund entity that are safe to keep in ``payments.raw_response``.
# Card details, VPA, email, phone, bank account data and customer notes are dropped.
SAFE_ENTITY_FIELDS = frozenset(
    {
        "id",
        "entity",
        "amount",
        "amount_paid",
        "amount_due",
        "amount_refunded",
        "currency",
        "status",
        "order_id",
        "payment_id",
        "receipt",
        "attempts",
        "method",
        "captured",
        "refund_status",
        "international",
        "fee",
        "tax",
        "bank",
        "wallet",
        "error_code",
        "error_description",
        "error_source",
        "error_step",
        "error_reason",
        "speed_processed",
        "speed_requested",
        "created_at",
    }
)


class RazorpayAPIError(ServiceUnavailableError):
    """A failed Razorpay call. ``message`` is always customer-safe; provider details are kept apart."""

    def __init__(
        self,
        message: str = UNAVAILABLE_MESSAGE,
        *,
        http_status: int | None = None,
        provider_code: str = "",
        provider_description: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.provider_code = provider_code
        self.provider_description = provider_description

    @property
    def is_client_error(self) -> bool:
        return self.http_status is not None and 400 <= self.http_status < 500


def to_paise(amount: Decimal | float | int | str) -> int:
    """Rupees → integer paise (ROUND_HALF_UP)."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sanitize_entity(entity: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only non-sensitive fields of a Razorpay entity."""
    if not isinstance(entity, dict):
        return {}
    return {key: value for key, value in entity.items() if key in SAFE_ENTITY_FIELDS}


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class RazorpayProvider:
    code = "razorpay"
    # Distinct from the direct "UPI" option (payments/upi.py) so the two never look alike at checkout.
    label = "Cards, netbanking & wallets (Razorpay)"
    description = "Secured by Razorpay. Pay instantly with UPI, credit/debit cards, netbanking or wallets."
    confirms_on_placement = False

    def __init__(
        self,
        key_id: str = "",
        key_secret: str = "",
        webhook_secret: str = "",
        *,
        base_url: str = API_BASE,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.key_id = key_id.strip()
        self._key_secret = key_secret.strip()
        self._webhook_secret = webhook_secret.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:  # never expose secrets through repr / logs
        return f"RazorpayProvider(key_id={self.key_id!r})"

    @classmethod
    def from_settings(cls, settings: Settings) -> "RazorpayProvider":
        return cls(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret.get_secret_value(),
            webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
            transport=HTTP_TRANSPORT,
        )

    # ------------------------------------------------------------------ PaymentProvider

    def is_configured(self, settings: Settings) -> bool:
        return settings.razorpay_configured

    def initiate(self, order: "Order") -> PaymentInitiation:
        """Create the Razorpay order. The shop order stays ``pending`` until the payment is verified."""
        try:
            rzp_order = self.create_order(order)
        except RazorpayAPIError as exc:
            raise RazorpayAPIError(
                CHECKOUT_UNAVAILABLE_MESSAGE,
                http_status=exc.http_status,
                provider_code=exc.provider_code,
                provider_description=exc.provider_description,
            ) from exc
        return PaymentInitiation(
            order_payment_status="pending",
            payment_status="created",
            reference=str(rzp_order.get("id", "")),
            raw_response={"razorpay_order": sanitize_entity(rzp_order)},
        )

    def verify(self, payload: dict[str, Any]) -> bool:
        return self.verify_payment_signature(
            str(payload.get("razorpay_order_id", "")),
            str(payload.get("razorpay_payment_id", "")),
            str(payload.get("razorpay_signature", "")),
        )

    # ------------------------------------------------------------------ API calls

    def create_order(self, order: "Order") -> dict[str, Any]:
        body = {
            "amount": to_paise(order.grand_total),
            "currency": CURRENCY,
            "receipt": order.order_number,
            "notes": {"order_id": str(order.id), "order_number": order.order_number},
        }
        created = self._request("POST", "/orders", json=body)
        if not created.get("id"):
            raise RazorpayAPIError(REJECTED_MESSAGE)
        return created

    def fetch_order(self, provider_order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{provider_order_id}")

    def fetch_order_payments(self, provider_order_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/orders/{provider_order_id}/payments")
        items = data.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/payments/{payment_id}")

    def capture_payment(self, payment_id: str, amount_paise: int, currency: str = CURRENCY) -> dict[str, Any]:
        return self._request("POST", f"/payments/{payment_id}/capture", json={"amount": amount_paise, "currency": currency})

    def refund(self, payment_id: str, amount_paise: int) -> dict[str, Any]:  # type: ignore[override]
        return self._request("POST", f"/payments/{payment_id}/refund", json={"amount": amount_paise})

    # ------------------------------------------------------------------ signatures

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Checkout handler signature: HMAC-SHA256(``order_id|payment_id``, key_secret)."""
        if not (self._key_secret and order_id and payment_id and signature):
            return False
        expected = _hmac_hex(self._key_secret, f"{order_id}|{payment_id}".encode("utf-8"))
        return hmac.compare_digest(expected, signature.strip())

    def verify_webhook_signature(self, raw_body: bytes, header: str | None) -> bool:
        """``X-Razorpay-Signature``: HMAC-SHA256(raw request body, webhook secret)."""
        if not (self._webhook_secret and header):
            return False
        expected = _hmac_hex(self._webhook_secret, raw_body)
        return hmac.compare_digest(expected, header.strip())

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if not (self.key_id and self._key_secret):
            raise RazorpayAPIError("Online payments are not available right now.")
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                auth=(self.key_id, self._key_secret),
                headers={"Accept": "application/json"},
            ) as client:
                response = client.request(method, url, json=json)
        except httpx.TimeoutException as exc:
            logger.warning("Razorpay API timeout: %s %s", method, path)
            raise RazorpayAPIError(TIMEOUT_MESSAGE) from exc
        except httpx.HTTPError as exc:
            logger.warning("Razorpay API network error: %s %s (%s)", method, path, type(exc).__name__)
            raise RazorpayAPIError(UNAVAILABLE_MESSAGE) from exc

        body = _safe_json(response)
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            provider_code = str(error.get("code") or "")
            description = str(error.get("description") or "")
            logger.error(
                "Razorpay API %s %s returned HTTP %s (%s: %s)",
                method,
                path,
                response.status_code,
                provider_code,
                description,
            )
            message = REJECTED_MESSAGE if response.status_code < 500 else UNAVAILABLE_MESSAGE
            raise RazorpayAPIError(
                message,
                http_status=response.status_code,
                provider_code=provider_code,
                provider_description=description,
            )
        return body


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
