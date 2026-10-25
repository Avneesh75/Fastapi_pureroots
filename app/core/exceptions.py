"""Application error hierarchy and handlers that render the standard error envelope.

Every error response looks like::

    {"success": false, "error": {"code": "...", "message": "...", "details": []}}
"""
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again."


class AppError(Exception):
    """Base class for expected, user-facing errors."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    default_message: str = GENERIC_ERROR_MESSAGE

    def __init__(self, message: str | None = None, *, details: list[Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details: list[Any] = list(details or [])
        super().__init__(self.message)


class ValidationAppError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"
    default_message = "Please check the highlighted fields and try again."


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    default_message = "The requested resource was not found."


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    default_message = "Please sign in to continue."


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    default_message = "You do not have permission to do that."


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    default_message = "This action conflicts with the current state."


class CouponNotCombinableError(AppError):
    """OFFERS_AND_ADMIN §3: a coupon with ``combinable_with_offers=False`` on a cart with offer discounts."""

    status_code = 409
    code = "COUPON_NOT_COMBINABLE"
    default_message = "This coupon can't be combined with ongoing offers."


class InsufficientStockError(AppError):
    status_code = 409
    code = "INSUFFICIENT_STOCK"
    default_message = "Some items are no longer available in the requested quantity."


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"
    default_message = "Too many attempts. Please wait a minute and try again."


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    default_message = "This service is temporarily unavailable. Please try again shortly."


class PaymentVerificationError(AppError):
    """The payment could not be verified (bad signature, wrong order, amount mismatch...)."""

    status_code = 400
    code = "PAYMENT_VERIFICATION_FAILED"
    default_message = (
        "We couldn't verify this payment. If money was debited, we'll confirm your order or refund you automatically."
    )


class WebhookSignatureError(AppError):
    status_code = 400
    code = "INVALID_SIGNATURE"
    default_message = "Invalid webhook signature."


class RefundFailedError(AppError):
    """The payment provider rejected a refund; the order status change was not applied."""

    status_code = 409
    code = "REFUND_FAILED"
    default_message = "The refund could not be issued, so the order was not changed. Please try again."


class InvalidFileError(AppError):
    """An upload that is not an accepted file type (e.g. not a real JPEG/PNG/WebP image)."""

    status_code = 400
    code = "INVALID_FILE"
    default_message = "Please upload a JPEG, PNG or WebP image."


class FileTooLargeError(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"
    default_message = "That file is too large."


class InternalError(AppError):
    status_code = 500
    code = "INTERNAL_ERROR"


# --------------------------------------------------------------------------- helpers


def error_payload(code: str, message: str, details: list[Any] | None = None) -> dict[str, Any]:
    """Build the error envelope body."""
    return {"success": False, "error": {"code": code, "message": message, "details": details or []}}


def error_json_response(exc: AppError) -> JSONResponse:
    """Render an ``AppError`` as a JSON response."""
    return JSONResponse(status_code=exc.status_code, content=error_payload(exc.code, exc.message, exc.details))


_LOCATION_ROOTS = {"body", "query", "path", "header", "cookie"}
_FRIENDLY_MESSAGES = {
    "missing": "This field is required.",
    "json_invalid": "The request body is not valid JSON.",
    "uuid_parsing": "Enter a valid identifier.",
    "uuid_type": "Enter a valid identifier.",
    "bool_parsing": "Enter true or false.",
    "int_parsing": "Enter a whole number.",
    "float_parsing": "Enter a number.",
}
_MESSAGE_PREFIXES = ("Value error, ", "Assertion failed, ")


def format_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert Pydantic errors into ``[{field, message}]`` with friendly wording."""
    details: list[dict[str, str]] = []
    for err in errors:
        location = [str(part) for part in err.get("loc", ())]
        if location and location[0] in _LOCATION_ROOTS:
            location = location[1:]
        field = ".".join(location) or "request"
        message = _FRIENDLY_MESSAGES.get(str(err.get("type", "")), str(err.get("msg", "Invalid value.")))
        for prefix in _MESSAGE_PREFIXES:
            if message.startswith(prefix):
                message = message[len(prefix):]
        details.append({"field": field, "message": message})
    return details


# --------------------------------------------------------------------------- handlers


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    if exc.status_code >= 500:
        logger.error("%s on %s %s: %s", exc.code, request.method, request.url.path, exc.message)
    return error_json_response(exc)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = format_validation_errors(list(exc.errors()))
    if len(details) == 1:
        message = details[0]["message"]
    else:
        message = ValidationAppError.default_message
    return JSONResponse(status_code=422, content=error_payload("VALIDATION_ERROR", message, details))


_HTTP_CODE_MAP = {
    400: ("VALIDATION_ERROR", "The request could not be processed."),
    401: ("UNAUTHORIZED", UnauthorizedError.default_message),
    403: ("FORBIDDEN", ForbiddenError.default_message),
    404: ("NOT_FOUND", NotFoundError.default_message),
    405: ("NOT_FOUND", "This method is not supported for this resource."),
    409: ("CONFLICT", ConflictError.default_message),
    413: ("FILE_TOO_LARGE", FileTooLargeError.default_message),
    429: ("RATE_LIMITED", RateLimitedError.default_message),
    503: ("SERVICE_UNAVAILABLE", ServiceUnavailableError.default_message),
}


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, default_message = _HTTP_CODE_MAP.get(
        exc.status_code,
        ("INTERNAL_ERROR" if exc.status_code >= 500 else "VALIDATION_ERROR", GENERIC_ERROR_MESSAGE),
    )
    message = exc.detail if isinstance(exc.detail, str) and exc.status_code not in (404, 405) else default_message
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(code, message),
        headers=getattr(exc, "headers", None),
    )


async def rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.info("Rate limit hit on %s", request.url.path)
    return JSONResponse(status_code=429, content=error_payload("RATE_LIMITED", RateLimitedError.default_message))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=error_payload("INTERNAL_ERROR", GENERIC_ERROR_MESSAGE))


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all envelope-producing handlers to the app."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
