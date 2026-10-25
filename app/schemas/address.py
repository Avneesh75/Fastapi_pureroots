"""Saved address models."""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.schemas.common import AppSchema, IndianPhone, IndianState, OrmSchema, PersonName, Pincode, Trimmed

Line = Annotated[str, Trimmed, Field(min_length=3, max_length=255)]
OptionalLine = Annotated[str, Trimmed, Field(max_length=255)]
City = Annotated[str, Trimmed, Field(min_length=2, max_length=100)]
Landmark = Annotated[str, Trimmed, Field(max_length=120)]
Label = Annotated[str, Trimmed, Field(min_length=1, max_length=50)]


class AddressFields(AppSchema):
    """The postal fields shared by saved addresses and the checkout form."""

    full_name: PersonName
    phone: IndianPhone
    line1: Line
    line2: OptionalLine = ""
    landmark: Landmark = ""
    city: City
    state: IndianState
    pincode: Pincode


class AddressCreate(AddressFields):
    label: Label = "Home"
    is_default: bool = False


class AddressUpdate(AppSchema):
    label: Label | None = None
    full_name: PersonName | None = None
    phone: IndianPhone | None = None
    line1: Line | None = None
    line2: OptionalLine | None = None
    landmark: Landmark | None = None
    city: City | None = None
    state: IndianState | None = None
    pincode: Pincode | None = None
    is_default: bool | None = None


class AddressOut(OrmSchema):
    id: uuid.UUID
    label: str
    full_name: str
    phone: str
    line1: str
    line2: str
    landmark: str
    city: str
    state: str
    pincode: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
