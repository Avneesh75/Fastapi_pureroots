"""Coupon validation against Django's ``coupons`` table and FastAPI's redemption log."""
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, utcnow
from app.core.exceptions import CouponNotCombinableError, ValidationAppError
from app.models.catalog import Coupon
from app.repositories import catalog_repository, order_repository
from app.services.pricing_service import ZERO, CouponTerms, format_inr, q2

INVALID_CODE = "This coupon code is not valid."
NOT_STARTED = "This coupon is not active yet."
EXPIRED = "This coupon has expired."
USAGE_EXHAUSTED = "This coupon has reached its usage limit."
USER_LIMIT_REACHED = "You have already used this coupon the maximum number of times."
NOT_COMBINABLE = CouponNotCombinableError.default_message


@dataclass(frozen=True)
class CouponCheck:
    """Outcome of evaluating a coupon code.

    ``keep`` is True when the coupon itself is fine but the cart does not qualify
    yet (minimum order); the cart keeps the code so it applies once eligible.
    ``not_combinable`` is True when the only problem is that the cart has offer discounts
    and the coupon has ``combinable_with_offers=False``.
    """

    code: str
    coupon: Coupon | None
    terms: CouponTerms | None
    error: str | None
    keep: bool
    not_combinable: bool = False

    @property
    def is_valid(self) -> bool:
        return self.error is None and self.terms is not None


def normalize_code(code: str) -> str:
    return code.strip().upper()


def terms_for(coupon: Coupon) -> CouponTerms:
    return CouponTerms(
        code=coupon.code.upper(),
        discount_type=coupon.discount_type,
        value=Decimal(coupon.value),
        min_order_value=Decimal(coupon.min_order_value or 0),
        max_discount=Decimal(coupon.max_discount) if coupon.max_discount is not None else None,
    )


def _failure(code: str, coupon: Coupon | None, error: str) -> CouponCheck:
    return CouponCheck(code=code, coupon=coupon, terms=None, error=error, keep=False)


def evaluate_coupon(
    db: Session,
    code: str,
    *,
    subtotal: Decimal,
    user_id: uuid.UUID | None,
    offer_discount: Decimal = ZERO,
    now: datetime | None = None,
) -> CouponCheck:
    """Evaluate ``code`` against the post-offer ``subtotal``.

    ``offer_discount`` is the cart's total offer discount; when it is > 0 a coupon that is not
    ``combinable_with_offers`` fails with ``not_combinable=True``.
    """
    normalized = normalize_code(code)
    coupon = catalog_repository.get_coupon_by_code(db, normalized)
    if coupon is None or not coupon.is_active or coupon.discount_type not in ("percent", "flat"):
        return _failure(normalized, None, INVALID_CODE)

    current = now or utcnow()
    valid_from = ensure_aware(coupon.valid_from)
    valid_until = ensure_aware(coupon.valid_until)
    if valid_from is not None and current < valid_from:
        return _failure(normalized, coupon, NOT_STARTED)
    if valid_until is not None and current > valid_until:
        return _failure(normalized, coupon, EXPIRED)
    if coupon.usage_limit is not None and order_repository.count_redemptions(db, coupon.id) >= coupon.usage_limit:
        return _failure(normalized, coupon, USAGE_EXHAUSTED)
    if (
        coupon.per_user_limit is not None
        and user_id is not None
        and order_repository.count_redemptions(db, coupon.id, user_id) >= coupon.per_user_limit
    ):
        return _failure(normalized, coupon, USER_LIMIT_REACHED)

    if q2(offer_discount) > ZERO and not coupon.combinable_with_offers:
        return CouponCheck(
            code=normalized, coupon=coupon, terms=None, error=NOT_COMBINABLE, keep=False, not_combinable=True
        )

    terms = terms_for(coupon)
    shortfall = q2(terms.min_order_value) - q2(subtotal)
    if shortfall > ZERO:
        message = f"Add {format_inr(shortfall)} more to use {terms.code}."
        return CouponCheck(code=normalized, coupon=coupon, terms=None, error=message, keep=True)
    return CouponCheck(code=normalized, coupon=coupon, terms=terms, error=None, keep=True)


def require_valid(check: CouponCheck) -> CouponCheck:
    """Raise ``COUPON_NOT_COMBINABLE`` (409) or a ``VALIDATION_ERROR`` on ``code`` when the coupon cannot be used."""
    if check.not_combinable:
        raise CouponNotCombinableError(details=[{"field": "code", "message": NOT_COMBINABLE}])
    if not check.is_valid:
        message = check.error or INVALID_CODE
        raise ValidationAppError(message, details=[{"field": "code", "message": message}])
    return check
