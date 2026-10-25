"""The ``PaymentProvider`` protocol every payment integration implements."""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.config import Settings

if TYPE_CHECKING:
    from app.models.order import Order


@dataclass(frozen=True)
class PaymentInitiation:
    """Result of starting a payment for a freshly created order.

    ``order_payment_status`` is written to ``orders.payment_status`` and
    ``payment_status`` to the ``payments`` row.
    """

    order_payment_status: str
    payment_status: str
    reference: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    redirect_url: str | None = None


@runtime_checkable
class PaymentProvider(Protocol):
    code: str
    label: str
    description: str
    # True (COD): the order is confirmed as soon as it is placed. False (online providers): the
    # order stays ``pending`` until the payment is verified.
    confirms_on_placement: bool

    def is_configured(self, settings: Settings) -> bool:
        """True when the env keys this provider needs are present."""
        ...

    def initiate(self, order: "Order") -> PaymentInitiation:
        """Start the payment (COD: nothing to do)."""
        ...

    def verify(self, payload: dict[str, Any]) -> bool:
        """Verify a provider callback / signature."""
        ...


@runtime_checkable
class OnlinePaymentProvider(PaymentProvider, Protocol):
    """A provider that collects money online (Razorpay). Amounts are in paise."""

    def create_order(self, order: "Order") -> dict[str, Any]:
        """Create the provider-side order for ``order.grand_total``."""
        ...

    def fetch_order(self, provider_order_id: str) -> dict[str, Any]: ...

    def fetch_order_payments(self, provider_order_id: str) -> list[dict[str, Any]]: ...

    def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    def capture_payment(self, payment_id: str, amount_paise: int, currency: str) -> dict[str, Any]: ...

    def refund(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Refund ``amount_paise`` of a captured payment."""
        ...

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook_signature(self, raw_body: bytes, header: str | None) -> bool: ...
