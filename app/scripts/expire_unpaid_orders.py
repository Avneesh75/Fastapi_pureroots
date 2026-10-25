"""Cancel unpaid online orders and release their stock.

* Razorpay orders not paid within ``PAYMENT_PENDING_EXPIRY_MINUTES``.
* Direct-UPI orders with no UTR submitted (or whose UTR was rejected) within ``UPI_PAYMENT_EXPIRY_MINUTES``.
  Orders whose UTR is awaiting verification are never cancelled.

Usage::

    python -m app.scripts.expire_unpaid_orders [--limit 200]

Safe to run from cron alongside the in-app sweep: each order is claimed atomically.
"""
import argparse
import logging
import sys

from app import database
from app.config import get_settings
from app.logging_config import configure_logging
from app.services import payment_service, upi_service
from app.services.inventory_client import InventoryClient

logger = logging.getLogger("app.scripts.expire_unpaid_orders")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cancel unpaid Razorpay and direct-UPI orders past their payment window.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum orders to process (default 200).")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    db = database.SessionLocal()
    inventory = InventoryClient.from_settings(settings)
    try:
        expired = payment_service.expire_unpaid_orders(db, inventory, settings, limit=args.limit)
        expired += upi_service.expire_unpaid_orders(db, inventory, settings, limit=args.limit)
    finally:
        db.close()
    logger.info("Expired %s unpaid order(s)", expired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
