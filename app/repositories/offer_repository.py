"""Read-only offer queries against Django's ``offers`` tables (OFFERS_AND_ADMIN §2–§3).

``live_rules`` returns ``pureroots_pricing.OfferRule`` objects in at most three queries:
one for the offers and one per M2M table (only when an offer with that scope is live).
The offer maths itself lives exclusively in the shared ``pureroots_pricing`` package.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from pureroots_pricing import OfferRule
from sqlalchemy import Table, or_, select
from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, utcnow
from app.models.catalog import Offer, offers_categories, offers_products


def _m2m_ids(
    db: Session, table: Table, column: str, offer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    if not offer_ids:
        return {}
    target = table.c[column]
    grouped: dict[uuid.UUID, set[str]] = defaultdict(set)
    for offer_id, target_id in db.execute(select(table.c.offer_id, target).where(table.c.offer_id.in_(offer_ids))):
        grouped[offer_id].add(str(target_id))
    return {offer_id: frozenset(ids) for offer_id, ids in grouped.items()}


def to_rule(
    offer: Offer, category_ids: frozenset[str] = frozenset(), product_ids: frozenset[str] = frozenset()
) -> OfferRule:
    return OfferRule(
        id=str(offer.id),
        name=offer.name,
        discount_type=offer.discount_type,
        value=Decimal(offer.value),
        scope=offer.scope,
        category_ids=category_ids,
        product_ids=product_ids,
        starts_at=ensure_aware(offer.starts_at),
        ends_at=ensure_aware(offer.ends_at),
        is_active=bool(offer.is_active),
        min_order_amount=Decimal(offer.min_order_amount) if offer.min_order_amount is not None else None,
        max_discount_amount=Decimal(offer.max_discount_amount) if offer.max_discount_amount is not None else None,
        priority=int(offer.priority or 0),
    )


def live_rules(db: Session, now: datetime | None = None) -> list[OfferRule]:
    """Offers live at ``now`` (active, started, not ended) as ``OfferRule``s.

    The SQL window filter is only a pre-filter; ``OfferRule.is_live`` stays the authority.
    """
    current = (ensure_aware(now) or utcnow()).astimezone(timezone.utc)
    stmt = (
        select(Offer)
        .where(
            Offer.is_active.is_(True),
            or_(Offer.starts_at.is_(None), Offer.starts_at <= current),
            or_(Offer.ends_at.is_(None), Offer.ends_at > current),
        )
        .order_by(Offer.priority.desc(), Offer.id)
    )
    offers = list(db.scalars(stmt))
    if not offers:
        return []
    categories = _m2m_ids(db, offers_categories, "category_id", [o.id for o in offers if o.scope == "category"])
    products = _m2m_ids(db, offers_products, "product_id", [o.id for o in offers if o.scope == "products"])
    rules = [
        to_rule(offer, categories.get(offer.id, frozenset()), products.get(offer.id, frozenset()))
        for offer in offers
    ]
    return [rule for rule in rules if rule.is_live(current)]

