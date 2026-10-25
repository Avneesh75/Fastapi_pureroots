"""/api/v1/checkout — static checkout options."""
from typing import Any

from fastapi import APIRouter

from app.core.responses import ApiResponse, envelope
from app.dependencies import SettingsDep
from app.schemas.order import CheckoutOptionsOut
from app.services import checkout_service

router = APIRouter(prefix="/checkout", tags=["Checkout"])


@router.get(
    "/options",
    response_model=ApiResponse[CheckoutOptionsOut],
    summary="Delivery methods, payment methods, free-shipping threshold and Indian states/UTs",
)
def checkout_options(settings: SettingsDep) -> dict[str, Any]:
    return envelope(checkout_service.get_checkout_options(settings))
