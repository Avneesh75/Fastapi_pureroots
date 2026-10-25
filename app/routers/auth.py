"""/api/v1/auth — registration, login, tokens, profile and password flows."""
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from sqlalchemy.orm import Session

from app.config import Settings
from app.core import security
from app.core.constants import CART_COOKIE, REFRESH_COOKIE
from app.core.exceptions import AppError, FileTooLargeError, error_json_response
from app.core.rate_limit import AVATAR_UPLOAD_LIMIT, FORGOT_PASSWORD_LIMIT, LOGIN_LIMIT, REGISTER_LIMIT, limiter
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CurrentUser, DbSession, EmailSenderDep, SettingsDep, get_optional_user_lenient
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UpdateMeRequest,
    UserOut,
)
from app.services import auth_service, avatar_service, cart_service
from app.services.auth_service import AuthResult
from app.services.email_service import send_safely

router = APIRouter(prefix="/auth", tags=["Auth"])

FORGOT_PASSWORD_MESSAGE = "If an account exists for that email, we've sent a link to reset your password."


def _complete_sign_in(db: Session, request: Request, response: Response, result: AuthResult, settings: Settings) -> None:
    """Set auth cookies and fold any guest cart into the user's cart."""
    security.set_auth_cookies(response, result.access_token, result.refresh_token, settings)
    guest_key = request.cookies.get(CART_COOKIE)
    if guest_key:
        cart_service.merge_guest_cart(db, result.user, guest_key)
        security.clear_cart_cookie(response, settings)


@router.post(
    "/register",
    response_model=ApiResponse[AuthResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer account",
    description=(
        "Creates the account, returns tokens and sets the `pr_access` / `pr_refresh` cookies. "
        "Password: at least 8 characters with a letter and a digit. Any guest cart is merged. "
        "Rate-limited to 5/minute per IP."
    ),
    responses=error_responses(403, 409, 422, 429),
)
@limiter.limit(REGISTER_LIMIT)
def register(
    request: Request, response: Response, payload: RegisterRequest, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    result = auth_service.register(db, payload, settings)
    _complete_sign_in(db, request, response, result, settings)
    return envelope(auth_service.to_response(result), message="Welcome to PureRoots!")


@router.post(
    "/login",
    response_model=ApiResponse[AuthResponse],
    summary="Sign in with email and password",
    description="Returns tokens, sets auth cookies and merges any guest cart. Rate-limited to 10/minute per IP.",
    responses=error_responses(401, 403, 422, 429),
)
@limiter.limit(LOGIN_LIMIT)
def login(
    request: Request, response: Response, payload: LoginRequest, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    result = auth_service.login(db, payload, settings)
    _complete_sign_in(db, request, response, result, settings)
    return envelope(auth_service.to_response(result), message=f"Welcome back, {result.user.first_name}!")


@router.post(
    "/refresh",
    response_model=ApiResponse[AuthResponse],
    summary="Exchange a refresh token for new tokens",
    description=(
        "Reads `refresh_token` from the body or the `pr_refresh` cookie and rotates both tokens. "
        "On failure the auth cookies are cleared so the browser falls back to guest mode."
    ),
    responses=error_responses(401, 403),
)
def refresh(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    payload: RefreshRequest | None = Body(default=None),
) -> Any:
    token = (payload.refresh_token if payload else None) or request.cookies.get(REFRESH_COOKIE)
    try:
        result = auth_service.refresh(db, token, settings)
    except AppError as exc:
        failure: JSONResponse = error_json_response(exc)
        security.clear_auth_cookies(failure, settings)
        return failure
    security.set_auth_cookies(response, result.access_token, result.refresh_token, settings)
    return envelope(auth_service.to_response(result))


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    summary="Sign out",
    description="Clears the auth cookies. With `all_devices=true` every issued token is revoked.",
    responses=error_responses(403),
)
def logout(
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    user: User | None = Depends(get_optional_user_lenient),
    all_devices: bool = Query(False, description="Also revoke tokens on every other device."),
) -> dict[str, Any]:
    if user is not None and all_devices:
        auth_service.logout_everywhere(db, user)
    security.clear_auth_cookies(response, settings)
    return envelope(None, message="You have been signed out.")


@router.get(
    "/me",
    response_model=ApiResponse[UserOut],
    summary="Current customer profile",
    responses=error_responses(401),
)
def read_me(user: CurrentUser) -> dict[str, Any]:
    return envelope(UserOut.model_validate(user))


@router.patch(
    "/me",
    response_model=ApiResponse[UserOut],
    summary="Update name / phone / date of birth / gender",
    description=(
        "Only the fields sent change. `date_of_birth` (YYYY-MM-DD) must make the customer 13–120 years old; "
        "`gender` is one of `female`, `male`, `other`, `prefer_not`. `null` clears phone, date_of_birth and gender."
    ),
    responses=error_responses(401, 403, 422),
)
def update_me(payload: UpdateMeRequest, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    updated = auth_service.update_profile(db, user, payload)
    return envelope(UserOut.model_validate(updated), message="Profile updated.")


# Multipart framing around the file; lets us refuse obviously oversized bodies from Content-Length alone.
_MULTIPART_OVERHEAD = 16 * 1024


@router.post(
    "/me/avatar",
    response_model=ApiResponse[UserOut],
    summary="Upload a profile photo",
    description=(
        "Multipart form with one `file` field. Only real JPEG, PNG or WebP images up to `AVATAR_MAX_BYTES` "
        "(default 2 MB) are accepted (400 `INVALID_FILE` / 413 `FILE_TOO_LARGE`). The photo is auto-rotated "
        "from EXIF, stripped of all metadata, centre-cropped to a square, resized to 512×512 and stored as WebP; "
        "the previous photo is deleted. Returns the updated profile. Rate-limited to 10/minute per IP."
    ),
    responses=error_responses(400, 401, 403, 413, 422, 429),
)
@limiter.limit(AVATAR_UPLOAD_LIMIT)
def upload_avatar(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    file: UploadFile = File(..., description="JPEG, PNG or WebP image"),
) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.avatar_max_bytes + _MULTIPART_OVERHEAD:
        raise FileTooLargeError(avatar_service.too_large_message(settings.avatar_max_bytes))
    try:
        updated = avatar_service.save_avatar(db, user, file.file, settings)
    finally:
        file.file.close()
    return envelope(UserOut.model_validate(updated), message="Profile photo updated.")


@router.delete(
    "/me/avatar",
    response_model=ApiResponse[UserOut],
    summary="Remove the profile photo",
    description="Deletes the stored file and clears `avatar_url` (the storefront falls back to initials).",
    responses=error_responses(401, 403),
)
def delete_avatar(user: CurrentUser, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    updated = avatar_service.delete_avatar(db, user, settings)
    return envelope(UserOut.model_validate(updated), message="Profile photo removed.")


@router.post(
    "/change-password",
    response_model=ApiResponse[AuthResponse],
    summary="Change password",
    description="Verifies the current password, signs out other devices and returns fresh tokens for this one.",
    responses=error_responses(401, 403, 422),
)
def change_password(
    payload: ChangePasswordRequest, response: Response, user: CurrentUser, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    result = auth_service.change_password(db, user, payload, settings)
    security.set_auth_cookies(response, result.access_token, result.refresh_token, settings)
    return envelope(auth_service.to_response(result), message="Your password has been changed.")


@router.post(
    "/forgot-password",
    response_model=ApiResponse[None],
    summary="Request a password reset link",
    description=(
        "Always returns 200 so accounts cannot be enumerated. In development (no `SMTP_HOST`) the "
        "reset link `${PUBLIC_SITE_URL}/account/reset-password/?token=...` is written to the log. "
        "Rate-limited to 5/minute per IP."
    ),
    responses=error_responses(422, 429),
)
@limiter.limit(FORGOT_PASSWORD_LIMIT)
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: DbSession,
    settings: SettingsDep,
    sender: EmailSenderDep,
) -> dict[str, Any]:
    email = auth_service.request_password_reset(db, payload.email, settings)
    if email is not None:
        background_tasks.add_task(send_safely, sender, email)
    return envelope(None, message=FORGOT_PASSWORD_MESSAGE)


@router.post(
    "/reset-password",
    response_model=ApiResponse[None],
    summary="Set a new password using a reset token",
    description="The token is single-use and expires after `PASSWORD_RESET_EXPIRE_MINUTES`. All sessions are revoked.",
    responses=error_responses(422),
)
def reset_password(payload: ResetPasswordRequest, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    auth_service.reset_password(db, payload.token, payload.new_password, settings)
    return envelope(None, message="Your password has been reset. Please sign in with your new password.")
