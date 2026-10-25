"""Credit PureRoots Coins for orders placed before the loyalty feature existed (demo data).

* Delivered orders of signed-in customers → ``earn_pending`` + ``earn_confirmed`` (coins become available).
* Open orders (pending … out_for_delivery) → ``earn_pending`` (credited when they are delivered).
* Cancelled / refunded orders and guest orders earn nothing.

Coins = floor(LOYALTY_EARN_PERCENT % × (subtotal − coupon discount − coins redeemed)), shipping excluded.
Idempotent: an order that already has any ledger row is skipped, so the script can be re-run safely.

Usage::

    python -m app.scripts.backfill_loyalty [--dry-run]
"""
import argparse
import logging
import sys

from sqlalchemy import exists, select

from app import database
from app.config import get_settings
from app.logging_config import configure_logging
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.services import loyalty_rules, loyalty_service

logger = logging.getLogger("app.scripts.backfill_loyalty")

OPEN_STATUSES = ("pending", "confirmed", "processing", "packed", "shipped", "out_for_delivery")
EARNING_STATUSES = (*OPEN_STATUSES, "delivered")


def backfill(db, settings, *, dry_run: bool = False) -> dict[str, int]:  # type: ignore[no-untyped-def]
    cfg = loyalty_rules.LoyaltyConfig.from_settings(settings)
    stats = {"orders": 0, "credited": 0, "pending": 0, "coins_available": 0, "coins_pending": 0, "skipped": 0}
    if not cfg.enabled:
        return stats
    has_rows = exists().where(LoyaltyTransaction.order_id == Order.id)
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.user_id.is_not(None), Order.status.in_(EARNING_STATUSES), ~has_rows)
            .order_by(Order.placed_at)
        )
    )
    for order in orders:
        coins = loyalty_rules.coins_to_earn(order.subtotal, order.discount_total, order.coins_redeemed or 0, cfg)
        if coins <= 0:
            stats["skipped"] += 1
            continue
        stats["orders"] += 1
        if order.status == "delivered":
            stats["credited"] += 1
            stats["coins_available"] += coins
        else:
            stats["pending"] += 1
            stats["coins_pending"] += coins
        if dry_run:
            continue
        order.coins_earned = coins
        loyalty_service.earn_pending_for_order(
            db, order.user_id, order, coins, note=f"Earned on order {order.order_number} (loyalty launch backfill)"
        )
        if order.status == "delivered":
            loyalty_service.confirm_for_order(db, order)
        db.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credit PureRoots Coins for existing orders (idempotent).")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be credited.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    db = database.SessionLocal()
    try:
        stats = backfill(db, settings, dry_run=args.dry_run)
    finally:
        db.close()
    prefix = "[dry run] " if args.dry_run else ""
    print(
        f"{prefix}{stats['orders']} order(s): {stats['credited']} delivered credited "
        f"({stats['coins_available']} coins available), {stats['pending']} open "
        f"({stats['coins_pending']} coins pending); {stats['skipped']} skipped (nothing to earn)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
