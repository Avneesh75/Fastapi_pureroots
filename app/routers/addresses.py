"""/api/v1/addresses — the signed-in customer's address book."""
import uuid
from typing import Any

from fastapi import APIRouter, status

from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CurrentUser, DbSession
from app.schemas.address import AddressCreate, AddressOut, AddressUpdate
from app.services import address_service

router = APIRouter(prefix="/addresses", tags=["Addresses"])


@router.get(
    "",
    response_model=ApiResponse[list[AddressOut]],
    summary="List saved addresses",
    description="Default address first, then newest.",
    responses=error_responses(401),
)
def list_addresses(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    addresses = address_service.list_addresses(db, user)
    return envelope([AddressOut.model_validate(address) for address in addresses])


@router.post(
    "",
    response_model=ApiResponse[AddressOut],
    status_code=status.HTTP_201_CREATED,
    summary="Save a new address",
    description="Phone `^[6-9]\\d{9}$`, PIN code `^[1-9]\\d{5}$`, state must be an Indian state/UT. "
    "The first address becomes the default.",
    responses=error_responses(401, 403, 422),
)
def create_address(payload: AddressCreate, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    address = address_service.create_address(db, user, payload)
    return envelope(AddressOut.model_validate(address), message="Address saved.")


@router.patch(
    "/{address_id}",
    response_model=ApiResponse[AddressOut],
    summary="Update an address",
    responses=error_responses(401, 403, 404, 422),
)
def update_address(address_id: uuid.UUID, payload: AddressUpdate, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    address = address_service.update_address(db, user, address_id, payload)
    return envelope(AddressOut.model_validate(address), message="Address updated.")


@router.delete(
    "/{address_id}",
    response_model=ApiResponse[None],
    summary="Delete an address",
    responses=error_responses(401, 403, 404),
)
def delete_address(address_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    address_service.delete_address(db, user, address_id)
    return envelope(None, message="Address removed.")
