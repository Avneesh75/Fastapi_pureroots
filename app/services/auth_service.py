"""Registration, login, token refresh, profile and password flows."""
import logging
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core import security
from app.core.clock import utcnow
from app.core.exceptions import ConflictError, ForbiddenError, UnauthorizedError, ValidationAppError
from app.models.user import User
from app.repositories import user_repository
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UpdateMeRequest,
    UserOut,
)
from app.services.email_service import OutgoingEmail, password_reset_email, reset_link

logger = logging.getLogger(__name__)

DUPLICATE_EMAIL = "An account with this email already exists. Try signing in instead."
BAD_CREDENTIALS = "Incorrect email or password."
ACCOUNT_DISABLED = "This account has been deactivated. Please contact support."
BAD_CURRENT_PASSWORD = "Your current password is incorrect."
INVALID_RESET_LINK = "This password reset link is invalid or has expired. Please request a new one."


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    refresh_token: str


@lru_cache
def _dummy_hash() -> str:
    """Used to keep login timing similar for unknown emails."""
    return security.hash_password("pureroot-timing-equaliser-0")


def issue_tokens(user: User, settings: Settings) -> AuthResult:
    return AuthResult(
        user=user,
        access_token=security.create_access_token(user.id, user.token_version, settings),
        refresh_token=security.create_refresh_token(user.id, user.token_version, settings),
    )


def to_response(result: AuthResult) -> AuthResponse:
    return AuthResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type="bearer",
        user=UserOut.model_validate(result.user),
    )


def register(db: Session, payload: RegisterRequest, settings: Settings) -> AuthResult:
    if user_repository.email_exists(db, payload.email):
        raise ConflictError(DUPLICATE_EMAIL, details=[{"field": "email", "message": DUPLICATE_EMAIL}])
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        phone=payload.phone,
        password_hash=security.hash_password(payload.password),
        last_login_at=utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(DUPLICATE_EMAIL, details=[{"field": "email", "message": DUPLICATE_EMAIL}]) from exc
    db.refresh(user)
    logger.info("New customer registered: %s", user.id)
    return issue_tokens(user, settings)


def login(db: Session, payload: LoginRequest, settings: Settings) -> AuthResult:
    user = user_repository.get_by_email(db, payload.email)
    if user is None:
        security.verify_password(payload.password, _dummy_hash())
        raise UnauthorizedError(BAD_CREDENTIALS)
    if not security.verify_password(payload.password, user.password_hash):
        raise UnauthorizedError(BAD_CREDENTIALS)
    if not user.is_active:
        raise ForbiddenError(ACCOUNT_DISABLED)
    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    return issue_tokens(user, settings)


def refresh(db: Session, refresh_token: str | None, settings: Settings) -> AuthResult:
    if not refresh_token:
        raise UnauthorizedError(security.SESSION_EXPIRED_MESSAGE)
    payload = security.decode_token(refresh_token, security.REFRESH_TOKEN_TYPE, settings)
    user = user_repository.get_by_id(db, payload.user_id)
    if user is None or not user.is_active or user.token_version != payload.token_version:
        raise UnauthorizedError(security.SESSION_EXPIRED_MESSAGE)
    return issue_tokens(user, settings)


def logout_everywhere(db: Session, user: User) -> None:
    """Invalidate every token issued to the user."""
    user.token_version += 1
    db.commit()


def update_profile(db: Session, user: User, payload: UpdateMeRequest) -> User:
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("full_name"):
        user.full_name = changes["full_name"]
    for field in ("phone", "date_of_birth", "gender"):
        if field in changes:
            setattr(user, field, changes[field])
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, user: User, payload: ChangePasswordRequest, settings: Settings) -> AuthResult:
    if not security.verify_password(payload.current_password, user.password_hash):
        raise ValidationAppError(
            BAD_CURRENT_PASSWORD, details=[{"field": "current_password", "message": BAD_CURRENT_PASSWORD}]
        )
    user.password_hash = security.hash_password(payload.new_password)
    user.token_version += 1  # sign out other devices
    db.commit()
    db.refresh(user)
    return issue_tokens(user, settings)


def request_password_reset(db: Session, email: str, settings: Settings) -> OutgoingEmail | None:
    """Build the reset email if the account exists. Callers always answer 200."""
    user = user_repository.get_by_email(db, email)
    if user is None or not user.is_active:
        return None
    token = security.create_password_reset_token(user.id, user.token_version, settings)
    link = reset_link(settings, token)
    return password_reset_email(user.email, user.full_name, link, settings.password_reset_expire_minutes)


def reset_password(db: Session, token: str, new_password: str, settings: Settings) -> None:
    try:
        payload = security.decode_token(token, security.RESET_TOKEN_TYPE, settings)
    except UnauthorizedError as exc:
        raise ValidationAppError(INVALID_RESET_LINK, details=[{"field": "token", "message": INVALID_RESET_LINK}]) from exc
    user = user_repository.get_by_id(db, payload.user_id)
    if user is None or not user.is_active or user.token_version != payload.token_version:
        raise ValidationAppError(INVALID_RESET_LINK, details=[{"field": "token", "message": INVALID_RESET_LINK}])
    user.password_hash = security.hash_password(new_password)
    user.token_version += 1  # single-use link + revoke existing sessions
    db.commit()
    logger.info("Password reset completed for user %s", user.id)
