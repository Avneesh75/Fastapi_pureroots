"""/api/v1/health — liveness + database check."""
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.responses import envelope
from app.dependencies import DbSession

router = APIRouter(tags=["Health"])
logger = logging.getLogger(__name__)


@router.get(
    "/health",
    summary="Health check",
    description="Returns 200 with `{status: ok, database: ok}`, or 503 when the database is unreachable.",
)
def health(db: DbSession) -> Any:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        logger.exception("Health check: database unreachable")
        return JSONResponse(status_code=503, content=envelope({"status": "degraded", "database": "unavailable"}))
    return envelope({"status": "ok", "database": "ok"})
