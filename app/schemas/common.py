"""Shared field types and validators (Indian phone, PIN code, state, password)."""
import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field

from app.core.constants import INDIAN_STATES

PHONE_PATTERN = re.compile(r"^[6-9]\d{9}$")
PINCODE_PATTERN = re.compile(r"^[1-9]\d{5}$")

PHONE_ERROR = "Enter a valid 10-digit Indian mobile number."
PINCODE_ERROR = "Enter a valid 6-digit PIN code."
STATE_ERROR = "Select a valid Indian state or union territory."
PASSWORD_ERROR = "Password must be at least 8 characters and include a letter and a number."

_STATE_LOOKUP = {state.lower(): state for state in INDIAN_STATES}


class AppSchema(BaseModel):
    """Base for request models: unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class OrmSchema(BaseModel):
    """Base for response models built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


def strip_text(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def normalize_phone(value: str) -> str:
    """Accept ``98765 43210``, ``+91-9876543210``, ``09876543210``; return 10 digits."""
    compact = re.sub(r"[\s\-().]", "", value or "")
    if compact.startswith("+91"):
        compact = compact[3:]
    elif len(compact) == 12 and compact.startswith("91"):
        compact = compact[2:]
    elif len(compact) == 11 and compact.startswith("0"):
        compact = compact[1:]
    if not PHONE_PATTERN.fullmatch(compact):
        raise ValueError(PHONE_ERROR)
    return compact


def validate_pincode(value: str) -> str:
    pincode = (value or "").replace(" ", "")
    if not PINCODE_PATTERN.fullmatch(pincode):
        raise ValueError(PINCODE_ERROR)
    return pincode


def normalize_state(value: str) -> str:
    """Case-insensitive match against the list of states/UTs; returns the canonical name."""
    canonical = _STATE_LOOKUP.get(" ".join((value or "").split()).lower())
    if canonical is None:
        raise ValueError(STATE_ERROR)
    return canonical


def validate_password_strength(value: str) -> str:
    if len(value) < 8 or not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise ValueError(PASSWORD_ERROR)
    return value


def _lower(value: str) -> str:
    return value.lower()


Trimmed = BeforeValidator(strip_text)

IndianPhone = Annotated[str, Trimmed, AfterValidator(normalize_phone)]
Pincode = Annotated[str, Trimmed, AfterValidator(validate_pincode)]
IndianState = Annotated[str, Trimmed, AfterValidator(normalize_state)]
Email = Annotated[EmailStr, Trimmed, AfterValidator(_lower)]
PersonName = Annotated[str, Trimmed, Field(min_length=2, max_length=120)]
StrongPassword = Annotated[str, Field(max_length=128), AfterValidator(validate_password_strength)]
