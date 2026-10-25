"""Auth request/response models."""
import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, Field, computed_field

from app.config import get_settings
from app.core.clock import IST, utcnow
from app.schemas.common import AppSchema, Email, IndianPhone, OrmSchema, PersonName, StrongPassword

GENDER_CHOICES = ("female", "male", "other", "prefer_not")
GENDER_ERROR = "Choose female, male, other or prefer not to say."

MIN_AGE = 13
MAX_AGE = 120
DOB_ERROR = f"Enter a valid date of birth (you must be between {MIN_AGE} and {MAX_AGE} years old)."


def age_on(dob: date, today: date) -> int:
    """Completed years between ``dob`` and ``today``."""
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_date_of_birth(value: date | None) -> date | None:
    if value is None:
        return None
    today = utcnow().astimezone(IST).date()
    if value > today or not MIN_AGE <= age_on(value, today) <= MAX_AGE:
        raise ValueError(DOB_ERROR)
    return value


def validate_gender(value: str) -> str:
    choice = value.strip().lower()
    if choice not in GENDER_CHOICES:
        raise ValueError(GENDER_ERROR)
    return choice


DateOfBirth = Annotated[date, AfterValidator(validate_date_of_birth)]
Gender = Annotated[str, AfterValidator(validate_gender)]


def media_url(path: str | None) -> str | None:
    """``{PUBLIC_MEDIA_BASE}/{path}`` for a stored relative media path, else None."""
    if not path:
        return None
    base = get_settings().public_media_base.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


class RegisterRequest(AppSchema):
    full_name: PersonName
    email: Email
    phone: IndianPhone | None = None
    password: StrongPassword


class LoginRequest(AppSchema):
    email: Email
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(AppSchema):
    refresh_token: str | None = None


class UserOut(OrmSchema):
    id: uuid.UUID
    email: str
    full_name: str
    first_name: str
    phone: str | None
    is_verified: bool
    created_at: datetime
    date_of_birth: date | None = None
    gender: str | None = None
    initials: str = ""
    coins_available: int = 0
    coins_pending: int = 0
    avatar: str | None = Field(default=None, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avatar_url(self) -> str | None:
        """Public URL of the 512×512 WebP profile photo, or null (show ``initials``)."""
        return media_url(self.avatar)


class AuthResponse(AppSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class UpdateMeRequest(AppSchema):
    """Only the fields sent are changed; ``null`` clears phone / date_of_birth / gender."""

    full_name: PersonName | None = None
    phone: IndianPhone | None = None
    date_of_birth: DateOfBirth | None = None
    gender: Gender | None = None


class ChangePasswordRequest(AppSchema):
    current_password: Annotated[str, Field(min_length=1, max_length=128)]
    new_password: StrongPassword


class ForgotPasswordRequest(AppSchema):
    email: Email


class ResetPasswordRequest(AppSchema):
    token: Annotated[str, Field(min_length=10, max_length=2048)]
    new_password: StrongPassword
