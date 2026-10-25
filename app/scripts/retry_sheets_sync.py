"""Retry Google Sheets sync for contact messages that failed (or are stuck in ``pending``).

Usage::

    python -m app.scripts.retry_sheets_sync [--failed-only] [--limit 200]
"""
import argparse
import logging
import sys

from app.config import get_settings
from app.logging_config import configure_logging
from app.services import contact_service
from app.services.lead_sink import build_lead_sink

logger = logging.getLogger("app.scripts.retry_sheets_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retry failed Google Sheets syncs for contact messages.")
    parser.add_argument("--failed-only", action="store_true", help="Skip messages stuck in 'pending'.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum messages to process (default 200).")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    sink = build_lead_sink(settings)
    if not sink.enabled:
        logger.error("Google Sheets sync is disabled or not configured (GOOGLE_SHEETS_ENABLED / credentials).")
        return 1

    counts = contact_service.retry_unsynced(sink, include_stale_pending=not args.failed_only, limit=args.limit)
    logger.info("Retry finished: %s synced, %s failed", counts.get("synced", 0), counts.get("failed", 0))
    return 0 if counts.get("failed", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
