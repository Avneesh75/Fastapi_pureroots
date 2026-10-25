"""FastAPI dependencies: DB session, authentication, cart context, internal token, gateways."""
import hmac
import re
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app import database
from app.config import Settings, get_settings
from app.core import security
from app.core.constants import ACCESS_COOKIE, CART_COOKIE
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.models.user import User
from app.repositories import user_repository
from app.services.cart_service import CartContext
from app.services.email_service import EmailSender, get_default_email_sender
from app.services.inventory_client import InventoryClient, InventoryGateway
from app.services.lead_sink import LeadSink, get_default_lead_sink

_SESSION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")


def get_db() -> Iterator[Session]:
    """One SQLAlchemy session per request."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --------------------------------------------------------------------------- auth


def _extract_token(request: Request) -> str | None:
    """Bearer header first, then the ``pr_access`` cookie."""
    authorization = request.headers.get("authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return request.cookies.get(ACCESS_COOKIE) or None


def _user_from_token(db: Session, token: str, settings: Settings) -> User:
    payload = security.decode_token(token, security.ACCESS_TOKEN_TYPE, settings)
    user = user_repository.get_by_id(db, payload.user_id)
    if user is None or not user.is_active or user.token_version != payload.token_version:
        raise UnauthorizedError(security.SESSION_EXPIRED_MESSAGE)
    return user


def get_current_user(request: Request, db: DbSession, settings: SettingsDep) -> User:
    """Require a valid access token (401 otherwise)."""
    token = _extract_token(request)
    if not token:
        raise UnauthorizedError("Please sign in to continue.")
    return _user_from_token(db, token, settings)


def get_optional_user(request: Request, db: DbSession, settings: SettingsDep) -> User | None:
    """Guest when no token is sent; 401 when a token is sent but invalid/expired (client refreshes)."""
    token = _extract_token(request)
    if not token:
        return None
    return _user_from_token(db, token, settings)


def get_optional_user_lenient(request: Request, db: DbSession, settings: SettingsDep) -> User | None:
    """Like ``get_optional_user`` but never raises (used by logout)."""
    token = _extract_token(request)
    if not token:
        return None
    try:
        return _user_from_token(db, token, settings)
    except UnauthorizedError:
        return None


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


# --------------------------------------------------------------------------- cart


def get_cart_context(request: Request, response: Response, user: OptionalUser, settings: SettingsDep) -> CartContext:
    """Resolve the cart owner. Guests get a ``pr_cart`` cookie (60 days) on first use.

    For signed-in users a leftover guest key is passed along so the cart service merges it;
    the cookie is removed in the same response.
    """
    session_key = request.cookies.get(CART_COOKIE)
    if session_key and not _SESSION_KEY_PATTERN.fullmatch(session_key):
        session_key = None
    if user is None and not session_key:
        session_key = security.generate_session_key()
        security.set_cart_cookie(response, session_key, settings)
    elif user is not None and session_key:
        security.clear_cart_cookie(response, settings)
    return CartContext(user=user, session_key=session_key)


CartCtx = Annotated[CartContext, Depends(get_cart_context)]


# --------------------------------------------------------------------------- internal


def require_internal_token(
    settings: SettingsDep,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """Service-to-service auth (constant-time comparison)."""
    if not x_internal_token:
        raise UnauthorizedError("Missing internal token.")
    if not hmac.compare_digest(x_internal_token.encode("utf-8"), settings.internal_api_token.encode("utf-8")):
        raise ForbiddenError("Invalid internal token.")


# --------------------------------------------------------------------------- gateways (overridden in tests)


def get_inventory_client(settings: SettingsDep) -> InventoryGateway:
    return InventoryClient.from_settings(settings)


def get_lead_sink() -> LeadSink:
    return get_default_lead_sink()


def get_email_sender() -> EmailSender:
    return get_default_email_sender()


Inventory = Annotated[InventoryGateway, Depends(get_inventory_client)]
LeadSinkDep = Annotated[LeadSink, Depends(get_lead_sink)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]
