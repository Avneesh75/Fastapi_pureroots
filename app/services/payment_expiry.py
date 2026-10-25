"""Periodic sweep that cancels online orders left unpaid (runs inside the FastAPI lifespan).

Every ``PAYMENT_EXPIRY_SWEEP_SECONDS`` (0 disables it) each worker process runs, in a thread,
``payment_service.expire_unpaid_orders`` (Razorpay, when configured) and
``upi_service.expire_unpaid_orders`` (direct-UPI orders with no UTR awaiting verification). Running it in several workers at once is
safe: every order is claimed with a conditional UPDATE, so exactly one worker cancels it.
The same sweep is available as ``python -m app.scripts.expire_unpaid_orders`` (cron).
"""
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import database
from app.config import Settings, get_settings
from app.services import payment_service, upi_service
from app.services.inventory_client import InventoryClient

logger = logging.getLogger(__name__)


def run_sweep_once(settings: Settings) -> int:
    """One sweep with its own DB session and inventory client."""
    db = database.SessionLocal()
    inventory = InventoryClient.from_settings(settings)
    try:
        expired = 0
        if settings.razorpay_configured:
            expired += payment_service.expire_unpaid_orders(db, inventory, settings)
        # UPI orders placed while UPI was on still expire after UPI_VPA is removed.
        expired += upi_service.expire_unpaid_orders(db, inventory, settings)
        return expired
    finally:
        db.close()


async def _sweep_forever(settings: Settings) -> None:
    interval = settings.payment_expiry_sweep_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(run_sweep_once, settings)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Unpaid-order expiry sweep failed")


def sweep_enabled(settings: Settings) -> bool:
    return settings.payment_expiry_sweep_seconds > 0 and (settings.razorpay_configured or settings.upi_configured)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    task: asyncio.Task[None] | None = None
    if sweep_enabled(settings):
        task = asyncio.create_task(_sweep_forever(settings), name="payment-expiry-sweep")
        logger.info("Unpaid-order expiry sweep every %ss", settings.payment_expiry_sweep_seconds)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
