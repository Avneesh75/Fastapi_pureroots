"""Maintenance mode for the storefront API.

While a super admin has maintenance mode on (``cms_site_settings.maintenance_enabled``,
owned by Django), every ``/api/v1/*`` request answers 503 with the error envelope
``{"success": false, "error": {"code": "MAINTENANCE", …}}`` and a ``Retry-After`` header,
EXCEPT the paths in ``ALLOWED_PREFIXES`` (health checks, service-to-service calls and
Razorpay callbacks, so in-flight payments can still complete).

The switch is read through the read-only ``SiteSettings`` mapping and cached for
``CACHE_TTL`` seconds. Any database error fails OPEN: it's logged and the API keeps
serving, because a broken maintenance check must never take the store down.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.clock import ensure_aware, utcnow
from app.core.constants import API_PREFIX
from app.core.exceptions import error_payload

logger = logging.getLogger(__name__)

ERROR_CODE = "MAINTENANCE"
CACHE_TTL = 5.0  # seconds
RETRY_AFTER_SECONDS = 600
ALLOWED_PREFIXES = (
    f"{API_PREFIX}/health/",
    f"{API_PREFIX}/internal/",
    f"{API_PREFIX}/payments/razorpay/",
)


@dataclass(frozen=True)
class MaintenanceState:
    enabled: bool = False
    brand_name: str = "PureRoots"
    eta: datetime | None = None


OFF = MaintenanceState()

_lock = threading.Lock()
_cache: tuple[float, MaintenanceState] | None = None


def reset_cache() -> None:
    """Forget the cached state (tests, or after a known change)."""
    global _cache
    with _lock:
        _cache = None


def is_exempt(path: str) -> bool:
    if not (path == API_PREFIX or path.startswith(f"{API_PREFIX}/")):
        return True  # docs, openapi.json, anything outside the storefront API
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def retry_after(state: MaintenanceState) -> int:
    """Seconds until the ETA (clamped to 1 min … 1 day), else a 10-minute default."""
    eta = ensure_aware(state.eta)
    if eta is not None:
        seconds = int((eta - utcnow()).total_seconds())
        if seconds > 0:
            return max(60, min(seconds, 86400))
    return RETRY_AFTER_SECONDS


def maintenance_response(state: MaintenanceState) -> JSONResponse:
    message = f"{state.brand_name or 'The store'} is down for scheduled maintenance. Please check back soon."
    return JSONResponse(
        status_code=503,
        content=error_payload(ERROR_CODE, message),
        headers={"Retry-After": str(retry_after(state)), "Cache-Control": "no-store"},
    )


def _read_state(session) -> MaintenanceState:
    from app.models.catalog import SiteSettings

    row = session.execute(
        select(SiteSettings.maintenance_enabled, SiteSettings.brand_name, SiteSettings.maintenance_eta).limit(1)
    ).first()
    if row is None:
        return OFF
    return MaintenanceState(enabled=bool(row[0]), brand_name=row[1] or "PureRoots", eta=row[2])


def _load_state(app) -> MaintenanceState:
    """Read the switch with a session from ``get_db`` (honouring dependency overrides)."""
    from app.dependencies import get_db

    overrides = getattr(app, "dependency_overrides", None) or {}
    provider = overrides.get(get_db, get_db)
    produced = provider()
    if inspect.isgenerator(produced):
        try:
            session = next(produced)
            try:
                return _read_state(session)
            finally:
                session.rollback()  # end the read-only transaction before returning the connection
        finally:
            produced.close()
    return _read_state(produced)


def _cached_state() -> MaintenanceState | None:
    with _lock:
        if _cache is not None and _cache[0] > time.monotonic():
            return _cache[1]
    return None


def _store(state: MaintenanceState) -> None:
    global _cache
    with _lock:
        _cache = (time.monotonic() + CACHE_TTL, state)


async def get_state(app) -> MaintenanceState:
    cached = _cached_state()
    if cached is not None:
        return cached
    try:
        state = await run_in_threadpool(_load_state, app)
    except Exception:  # noqa: BLE001 - fail open
        logger.exception("Maintenance check failed; serving the API normally.")
        state = OFF
    _store(state)  # errors are cached too, so a DB outage isn't hammered (or logged) per request
    return state


class MaintenanceMiddleware:
    """Pure ASGI middleware: 503 ``MAINTENANCE`` for ``/api/v1/*`` while the switch is on."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() == "OPTIONS" or is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        state = await get_state(scope.get("app"))
        if state.enabled:
            await maintenance_response(state)(scope, receive, send)
            return
        await self.app(scope, receive, send)
