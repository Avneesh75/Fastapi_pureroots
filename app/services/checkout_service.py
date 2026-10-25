"""Checkout options: delivery methods, payment methods, free-shipping threshold and states."""
from app.config import Settings
from app.core.constants import INDIAN_STATES
from app.schemas.order import CheckoutOptionsOut, DeliveryMethodOut, PaymentMethodOut
from app.services.payments import registry
from app.services.pricing_service import format_inr, money


def get_checkout_options(settings: Settings) -> CheckoutOptionsOut:
    threshold = settings.free_shipping_threshold
    delivery_methods = [
        DeliveryMethodOut(
            code="standard",
            label="Standard delivery",
            fee=money(settings.standard_shipping_fee),
            eta="3-6 business days",
            description=f"Free on orders of {format_inr(threshold)} or more.",
        ),
        DeliveryMethodOut(
            code="express",
            label="Express delivery",
            fee=money(settings.express_shipping_fee),
            eta="1-2 business days",
            description="Priority dispatch within 24 hours in metro cities.",
        ),
    ]
    # Only configured providers, in registry order: COD, then direct UPI (UPI_VPA set), then Razorpay
    # (keys set). Nothing unavailable or "coming soon" is listed.
    payment_methods = [
        PaymentMethodOut(code=provider.code, label=provider.label, description=provider.description, enabled=True)
        for provider in registry.available_providers(settings)
    ]
    return CheckoutOptionsOut(
        delivery_methods=delivery_methods,
        payment_methods=payment_methods,
        free_shipping_threshold=money(threshold),
        states=list(INDIAN_STATES),
    )
