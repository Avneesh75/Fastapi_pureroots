"""Rate limiting (slowapi). Disabled entirely when ``RATE_LIMIT_ENABLED=false``."""
from slowapi import Limiter
from starlette.requests import Request

from app.config import get_settings

LOGIN_LIMIT = "10/minute"
REGISTER_LIMIT = "5/minute"
FORGOT_PASSWORD_LIMIT = "5/minute"
CONTACT_LIMIT = "5/minute"
# Guessing a UTR for someone else's order needs its number and email; this keeps retries cheap but slow.
UPI_SUBMIT_LIMIT = "10/minute"
AVATAR_UPLOAD_LIMIT = "10/minute"


def client_ip(request: Request) -> str:
    """Best-effort client IP. nginx sets ``X-Real-IP`` / ``X-Forwarded-For`` in front of FastAPI."""
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(
    key_func=client_ip,
    enabled=get_settings().rate_limit_enabled,
    headers_enabled=False,
)
