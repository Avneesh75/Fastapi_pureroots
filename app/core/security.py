"""Password hashing (bcrypt), JWT tokens and auth/cart cookie helpers.

Every token carries ``sub`` (user id), ``tv`` (the user's ``token_version``) and
``type`` (``access`` | ``refresh`` | ``password_reset``). Bumping
``users.token_version`` revokes every token issued before — used by logout-all,
change-password and password reset (which makes reset links single-use).
"""
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Response

from app.config import Settings, get_settings
from app.core.clock import utcnow
from app.core.constants import (
    ACCESS_COOKIE,
    CART_COOKIE,
    CART_COOKIE_MAX_AGE,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
)
from app.core.exceptions import UnauthorizedError

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
RESET_TOKEN_TYPE = "password_reset"

SESSION_EXPIRED_MESSAGE = "Your session has expired. Please sign in again."
INVALID_TOKEN_MESSAGE = "Your session is no longer valid. Please sign in again."

#: bcrypt cost factor. Tests lower this to keep the suite fast.
BCRYPT_ROUNDS = 12
_BCRYPT_MAX_BYTES = 72


# --------------------------------------------------------------------------- passwords


def _password_bytes(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes; newer bcrypt releases raise instead of truncating.
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time password check; returns False for malformed hashes."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- tokens


@dataclass(frozen=True)
class TokenPayload:
    user_id: uuid.UUID
    token_version: int
    token_type: str


def _encode(user_id: uuid.UUID, token_version: int, token_type: str, lifetime: timedelta, settings: Settings) -> str:
    now = utcnow()
    claims = {
        "sub": str(user_id),
        "tv": int(token_version),
        "type": token_type,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID, token_version: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    return _encode(user_id, token_version, ACCESS_TOKEN_TYPE, lifetime, settings)


def create_refresh_token(user_id: uuid.UUID, token_version: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    lifetime = timedelta(days=settings.refresh_token_expire_days)
    return _encode(user_id, token_version, REFRESH_TOKEN_TYPE, lifetime, settings)


def create_password_reset_token(user_id: uuid.UUID, token_version: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    lifetime = timedelta(minutes=settings.password_reset_expire_minutes)
    return _encode(user_id, token_version, RESET_TOKEN_TYPE, lifetime, settings)


def decode_token(token: str, expected_type: str, settings: Settings | None = None) -> TokenPayload:
    """Validate signature, expiry and type. Raises ``UnauthorizedError`` on any problem."""
    settings = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(SESSION_EXPIRED_MESSAGE) from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError(INVALID_TOKEN_MESSAGE) from exc

    if claims.get("type") != expected_type:
        raise UnauthorizedError(INVALID_TOKEN_MESSAGE)
    try:
        user_id = uuid.UUID(str(claims["sub"]))
        token_version = int(claims["tv"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError(INVALID_TOKEN_MESSAGE) from exc
    return TokenPayload(user_id=user_id, token_version=token_version, token_type=expected_type)


def generate_session_key() -> str:
    """Random guest-cart session key (43 URL-safe chars, fits ``carts.session_key``)."""
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- cookies


def set_auth_cookies(response: Response, access_token: str, refresh_token: str, settings: Settings) -> None:
    """Set ``pr_access`` (path /) and ``pr_refresh`` (path /api/v1/auth), both httpOnly + SameSite=Lax.

    The access cookie lives as long as the refresh token so an *expired* access token is
    still sent; the API answers 401 and the client refreshes once.
    """
    max_age = settings.refresh_token_expire_days * 24 * 60 * 60
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, secure=settings.cookie_secure, httponly=True, samesite="lax"
    )


def set_cart_cookie(response: Response, session_key: str, settings: Settings) -> None:
    response.set_cookie(
        CART_COOKIE,
        session_key,
        max_age=CART_COOKIE_MAX_AGE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_cart_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(CART_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
