"""Response envelope helpers and OpenAPI models.

Success: ``{"success": true, "data": ..., "meta": null, "message": null}``
"""
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standard success envelope. Used as ``response_model`` so OpenAPI shows ``data``'s schema."""

    success: bool = True
    data: T | None = None
    meta: dict[str, Any] | None = None
    message: str | None = None


class ErrorBody(BaseModel):
    code: str = Field(examples=["VALIDATION_ERROR"])
    message: str = Field(examples=["Please check the highlighted fields and try again."])
    details: list[Any] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    success: bool = False
    error: ErrorBody


def envelope(data: Any = None, *, meta: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    """Wrap a payload in the success envelope."""
    return {"success": True, "data": data, "meta": meta, "message": message}


_ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "Bad request (`PAYMENT_VERIFICATION_FAILED`, `INVALID_SIGNATURE`, `INVALID_FILE`).",
    401: "`UNAUTHORIZED` — not signed in or the session expired.",
    403: "`FORBIDDEN` — not allowed (also returned when the CSRF header is missing on cookie-authenticated requests).",
    404: "`NOT_FOUND` — the resource does not exist or is not yours.",
    409: "`CONFLICT` / `INSUFFICIENT_STOCK` / `REFUND_FAILED` — the request conflicts with the current state.",
    413: "`FILE_TOO_LARGE` — the upload is over the size limit.",
    422: "`VALIDATION_ERROR` — see `error.details[]` for `{field, message}` pairs.",
    429: "`RATE_LIMITED` — too many requests; retry in a minute.",
    503: "`SERVICE_UNAVAILABLE` — a dependent service (e.g. inventory or Razorpay) is unreachable.",
}


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries documenting the error envelope for the given codes."""
    return {
        code: {"model": ErrorResponse, "description": _ERROR_DESCRIPTIONS.get(code, "Error.")}
        for code in status_codes
    }
