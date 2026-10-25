"""Cash on Delivery provider."""
import logging
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.services.payments.base import PaymentInitiation

if TYPE_CHECKING:
    from app.models.order import Order

logger = logging.getLogger(__name__)


class CashOnDeliveryProvider:
    code = "cod"
    label = "Cash on Delivery"
    description = "Pay in cash or by UPI when your order arrives."
    confirms_on_placement = True

    def is_configured(self, settings: Settings) -> bool:
        return True

    def initiate(self, order: "Order") -> PaymentInitiation:
        return PaymentInitiation(
            order_payment_status="cod_pending",
            payment_status="pending",
            reference="",
            raw_response={"method": "cod"},
        )

    def verify(self, payload: dict[str, Any]) -> bool:
        return True

    def refund(self, order: "Order") -> None:
        # COD refunds are settled offline by the support team.
        logger.info("COD refund for order %s to be settled offline", order.order_number)
