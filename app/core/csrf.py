"""CSRF defence for cookie-authenticated requests (ARCHITECTURE §5).

Unsafe requests (POST/PUT/PATCH/DELETE) that carry one of our cookies and no
``Authorization: Bearer`` header must send ``X-Requested-With: PureRoot``.
A cross-site form cannot set custom headers, and a cross-site ``fetch`` with
custom headers needs a CORS preflight that our allow-list rejects.
"""
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.constants import (
    ACCESS_COOKIE,
    API_PREFIX,
    CART_COOKIE,
    CSRF_HEADER,
    CSRF_HEADER_VALUE,
    REFRESH_COOKIE,
)
from app.core.exceptions import error_payload

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
SESSION_COOKIES = (ACCESS_COOKIE, REFRESH_COOKIE, CART_COOKIE)
# Internal service calls, and the Razorpay webhook (server-to-server, authenticated by its HMAC signature).
CSRF_EXEMPT_PREFIXES = (f"{API_PREFIX}/internal", f"{API_PREFIX}/payments/razorpay/webhook")
CSRF_ERROR_MESSAGE = "Request blocked for your security. Please refresh the page and try again."


class CSRFMiddleware:
    """Pure ASGI middleware enforcing the ``X-Requested-With`` header."""

    def __init__(self, app: ASGIApp, exempt_prefixes: tuple[str, ...] = CSRF_EXEMPT_PREFIXES) -> None:
        self.app = app
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path.startswith(prefix) for prefix in self.exempt_prefixes):
            request = Request(scope)
            if self._is_cookie_authenticated(request) and request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
                response = JSONResponse(status_code=403, content=error_payload("FORBIDDEN", CSRF_ERROR_MESSAGE))
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _is_cookie_authenticated(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return False
        return any(request.cookies.get(name) for name in SESSION_COOKIES)
