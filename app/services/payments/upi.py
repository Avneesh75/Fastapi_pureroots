"""Direct UPI: the customer pays the store's own UPI ID (``UPI_VPA``) from any UPI app. No gateway.

Enabled only when ``UPI_VPA`` is set and looks like a UPI ID (``name@bank``). The shop order stays
``pending`` until staff confirm, in the admin panel, that the money arrived:

    placed            → payment_status=pending,               payments.status=awaiting_payment
    UTR submitted     → payment_status=awaiting_verification, payments.status=submitted
    admin approves    → payment_status=paid (order confirmed), payments.status=paid
    admin rejects     → payment_status=failed (customer may resubmit), payments.status=rejected

The deep link follows the NPCI "UPI linking specification":
``upi://pay?pa=<vpa>&pn=<payee>&am=<amount>&cu=INR&tn=<note>&tr=<order number>``. The amount is
always the server-computed ``orders.grand_total``.
"""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.config import UPI_VPA_PATTERN, Settings
from app.services.payments.base import PaymentInitiation

if TYPE_CHECKING:
    from app.models.order import Order

PROVIDER_CODE = "upi"
CURRENCY = "INR"
DEFAULT_PAYEE_NAME = "PureRoots Aahar"

UTR_PATTERN = re.compile(r"^\d{12}$")
UTR_MESSAGE = "Enter the 12-digit UPI transaction ID (UTR) from your UPI app."
PAYER_VPA_MESSAGE = "Enter the UPI ID you paid from, like name@okbank, or leave it empty."

# payments.status values for a UPI row.
AWAITING_PAYMENT = "awaiting_payment"
SUBMITTED = "submitted"
PAID = "paid"
REJECTED = "rejected"
EXPIRED = "expired"
REFUNDED = "refunded"


def is_valid_vpa(value: str | None) -> bool:
    return bool(value and UPI_VPA_PATTERN.fullmatch(value.strip()))


def normalize_utr(value: str | None) -> str:
    """Spaces/dashes that some apps show inside the UTR are dropped (``1234 5678 9012`` → ``123456789012``)."""
    return re.sub(r"[\s-]", "", value or "")


def is_valid_utr(value: str | None) -> bool:
    return bool(UTR_PATTERN.fullmatch(normalize_utr(value)))


def format_amount(amount: Decimal | float | int | str) -> str:
    """Rupees with exactly two decimals (ROUND_HALF_UP), as UPI's ``am`` expects: ``878.00``."""
    return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def transaction_note(order_number: str) -> str:
    return f"PureRoots order {order_number}"


def build_upi_uri(vpa: str, payee_name: str, amount: Decimal | float | str, order_number: str) -> str:
    """``upi://pay`` deep link. Values are percent-encoded (spaces as ``%20``; UPI apps don't read ``+``)."""
    params = [
        ("pa", quote(vpa.strip(), safe="@.-_")),
        ("pn", quote(payee_name.strip() or DEFAULT_PAYEE_NAME, safe="")),
        ("am", format_amount(amount)),
        ("cu", CURRENCY),
        ("tn", quote(transaction_note(order_number), safe="")),
        ("tr", quote(order_number, safe="-")),
    ]
    return "upi://pay?" + "&".join(f"{key}={value}" for key, value in params)


@dataclass(frozen=True)
class DirectUpiProvider:
    vpa: str = ""
    payee_name: str = DEFAULT_PAYEE_NAME

    code = PROVIDER_CODE
    label = "UPI"
    description = "Pay instantly with GPay, PhonePe, Paytm, BHIM or any UPI app"
    # The order is confirmed by staff once the money is seen in the bank/UPI app.
    confirms_on_placement = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "DirectUpiProvider":
        return cls(vpa=settings.upi_vpa_clean, payee_name=settings.upi_payee_name.strip() or DEFAULT_PAYEE_NAME)

    def is_configured(self, settings: Settings) -> bool:
        return settings.upi_configured

    def uri_for(self, order: "Order") -> str:
        return build_upi_uri(self.vpa, self.payee_name, order.grand_total, order.order_number)

    def initiate(self, order: "Order") -> PaymentInitiation:
        # The VPA and payee shown to the customer are kept on the row, so the order stays payable to the
        # same UPI ID even if UPI_VPA changes later.
        return PaymentInitiation(
            order_payment_status="pending",
            payment_status=AWAITING_PAYMENT,
            reference="",
            raw_response={"method": PROVIDER_CODE, "vpa": self.vpa, "payee_name": self.payee_name},
        )

    def verify(self, payload: dict[str, Any]) -> bool:
        """A UPI "callback" is the customer's UTR: only its shape can be checked automatically."""
        return is_valid_utr(str(payload.get("utr", "")))
