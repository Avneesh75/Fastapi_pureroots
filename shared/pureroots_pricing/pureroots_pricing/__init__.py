"""PureRoots shared offer maths (see docs/OFFERS_AND_ADMIN.md §1).

Pure Python, no dependencies. Imported by the Django storefront (catalog display)
and by FastAPI (cart / checkout) so both always compute identical prices.
"""

from .offers import (
    CartOfferResult,
    LineInput,
    OfferRule,
    PricedLine,
    PricedUnit,
    conditional_offers,
    format_inr,
    offer_hint,
    offer_label,
    price_lines,
    price_unit,
    validate_offer,
)

__version__ = "1.0.0"

__all__ = [
    "OfferRule",
    "LineInput",
    "PricedUnit",
    "PricedLine",
    "CartOfferResult",
    "price_unit",
    "price_lines",
    "conditional_offers",
    "validate_offer",
    "offer_label",
    # Additive helpers (not in the original contract list, safe to ignore):
    "offer_hint",
    "format_inr",
]
