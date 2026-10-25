"""Payment provider registry.

A provider implements ``PaymentProvider`` (``base.py``), reads its env keys in ``is_configured``
and is listed in ``PROVIDER_CLASSES``. Only providers whose configuration is present are listed
and accepted at checkout: direct UPI needs a valid ``UPI_VPA``; Razorpay needs ``RAZORPAY_KEY_ID`` and
``RAZORPAY_KEY_SECRET``. The order of ``PROVIDER_CLASSES`` is the order shown at checkout.
Classes with a ``from_settings`` factory are built from the settings; others with no arguments.
"""
from app.config import Settings, get_settings
from app.core.exceptions import ValidationAppError
from app.services.payments.base import PaymentProvider
from app.services.payments.cod import CashOnDeliveryProvider
from app.services.payments.razorpay import RazorpayProvider
from app.services.payments.upi import DirectUpiProvider

PROVIDER_CLASSES: tuple[type, ...] = (CashOnDeliveryProvider, DirectUpiProvider, RazorpayProvider)

UNAVAILABLE_MESSAGE = "This payment method is not available."


def available_providers(settings: Settings | None = None) -> list[PaymentProvider]:
    settings = settings or get_settings()
    providers: list[PaymentProvider] = [_build(cls, settings) for cls in PROVIDER_CLASSES]
    return [provider for provider in providers if provider.is_configured(settings)]


def _build(cls: type, settings: Settings) -> PaymentProvider:
    factory = getattr(cls, "from_settings", None)
    return factory(settings) if callable(factory) else cls()


def get_provider(code: str, settings: Settings | None = None) -> PaymentProvider:
    normalized = (code or "").strip().lower()
    for provider in available_providers(settings):
        if provider.code == normalized:
            return provider
    raise ValidationAppError(UNAVAILABLE_MESSAGE, details=[{"field": "payment_method", "message": UNAVAILABLE_MESSAGE}])
