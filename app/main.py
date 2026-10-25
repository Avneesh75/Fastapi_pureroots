"""FastAPI application factory. Run with ``uvicorn app.main:app --port 8001``."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models as _models  # noqa: F401  (registers every mapper before first use)
from app.config import Settings, get_settings
from app.core.constants import API_PREFIX, CSRF_HEADER
from app.core.csrf import CSRFMiddleware
from app.core.exceptions import register_exception_handlers
from app.core.maintenance import MaintenanceMiddleware
from app.core.rate_limit import limiter
from app.logging_config import configure_logging
from app.routers import (
    addresses,
    auth,
    cart,
    categories,
    checkout,
    contact,
    health,
    internal,
    loyalty,
    newsletter,
    orders,
    payments,
    products,
    search,
    wishlist,
)
from app.services.payment_expiry import lifespan as payment_expiry_lifespan

API_DESCRIPTION = """
The PureRoots storefront API (organic foods, India). Consumed by the React islands rendered by Django.

**Envelope.** Every response is `{"success": true, "data": …, "meta": …, "message": …}` or
`{"success": false, "error": {"code", "message", "details"}}`. Error codes:
`VALIDATION_ERROR, NOT_FOUND, UNAUTHORIZED, FORBIDDEN, CONFLICT, INSUFFICIENT_STOCK, RATE_LIMITED,
SERVICE_UNAVAILABLE, INTERNAL_ERROR`.

**Auth.** Send `Authorization: Bearer <access_token>` or rely on the httpOnly `pr_access` cookie.
Cookie-authenticated `POST/PUT/PATCH/DELETE` requests must include `X-Requested-With: PureRoot`.

**Money** is a JSON number in INR; prices include GST.
"""

OPENAPI_TAGS = [
    {"name": "Auth", "description": "Accounts, JWT tokens (also set as cookies), profile and password reset."},
    {"name": "Addresses", "description": "The signed-in customer's address book."},
    {"name": "Catalog", "description": "Categories, products, facets, related products and reviews."},
    {"name": "Search", "description": "Full-text product search and type-ahead suggestions."},
    {"name": "Cart", "description": "Guest (`pr_cart` cookie) or customer cart with live stock checks and coupons."},
    {"name": "Wishlist", "description": "Saved products (sign-in required)."},
    {"name": "Checkout", "description": "Delivery/payment options and the list of Indian states & UTs."},
    {"name": "Orders", "description": "Placing orders, order history, guest lookup and cancellation."},
    {"name": "Loyalty", "description": "PureRoots Coins: 30% back as coins on delivered orders, redeemable at checkout."},
    {"name": "Payments", "description": "Razorpay Checkout verification and the Razorpay webhook."},
    {"name": "Contact", "description": "Contact-us enquiries, synced to Google Sheets."},
    {"name": "Newsletter", "description": "Newsletter subscriptions."},
    {"name": "Internal", "description": "Service-to-service endpoints protected by `X-Internal-Token`."},
    {"name": "Health", "description": "Liveness / readiness."},
]

ROUTERS = (
    auth.router,
    addresses.router,
    categories.router,
    products.router,
    search.router,
    cart.router,
    wishlist.router,
    checkout.router,
    orders.router,
    loyalty.router,
    payments.router,
    contact.router,
    newsletter.router,
    internal.router,
    health.router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    application = FastAPI(
        title="PureRoots API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=payment_expiry_lifespan,
    )
    application.state.limiter = limiter
    register_exception_handlers(application)

    # Order matters: the last middleware added is the outermost. CORS wraps CSRF so that
    # CSRF rejections still carry CORS headers.
    application.add_middleware(CSRFMiddleware)
    # Maintenance mode (503 MAINTENANCE on /api/v1/*): inside CORS so the 503 carries CORS headers.
    application.add_middleware(MaintenanceMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", CSRF_HEADER],
    )

    for router in ROUTERS:
        application.include_router(router, prefix=API_PREFIX)

    if settings.serve_media:
        # Customer avatars live on FastAPI's own disk (MEDIA_ROOT/avatars). Behind nginx the
        # shared volume serves them; on separate hosts (Render) this app serves them itself.
        from fastapi.staticfiles import StaticFiles

        avatars_dir = settings.media_root / "avatars"
        avatars_dir.mkdir(parents=True, exist_ok=True)
        application.mount("/media/avatars", StaticFiles(directory=avatars_dir), name="avatars")
    return application


app = create_app()
