"""Application settings, loaded from environment variables (see ``.env.example``)."""
import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Layout: <repo>/backend/api/app/config.py  (in Docker: /app/app/config.py, so guard the depth).
_API_DIR = Path(__file__).resolve().parents[1]  # backend/api
_BACKEND_DIR = _API_DIR.parent  # backend
_REPO_ROOT = _BACKEND_DIR.parent  # repository root
# Repo-root .env (for local runs outside Docker), then an api-local .env. Missing files are
# ignored; real environment variables always take precedence over both.
REPO_ROOT_ENV = _REPO_ROOT / ".env"
BACKEND_ENV = _API_DIR / ".env"
# Uploaded customer files (avatars). Defaults to Django's MEDIA_ROOT (backend/web/media) so Django
# serves them in dev; in Docker both services mount the shared ``media`` volume at the same path.
DEFAULT_MEDIA_ROOT = _BACKEND_DIR / "web" / "media"

_ALLOWED_JWT_ALGORITHMS = {"HS256", "HS384", "HS512"}

# A UPI virtual payment address (VPA / UPI ID), e.g. ``pureroots@okicici``.
UPI_VPA_PATTERN = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z][a-zA-Z0-9.\-]{1,64}$")


class Settings(BaseSettings):
    """Typed view of the environment. Names match ``.env.example`` exactly."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT_ENV, BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Shared ----
    environment: str = "development"
    public_site_url: str = "http://localhost:8080"
    internal_api_token: str = Field(min_length=16)

    # ---- Database ----
    database_url: str = "postgresql+psycopg://pureroot:pureroot@localhost:5432/pureroot"

    # ---- Auth ----
    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=30, ge=1)
    refresh_token_expire_days: int = Field(default=14, ge=1)
    password_reset_expire_minutes: int = Field(default=30, ge=5)
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:8080,http://localhost:8000"

    # ---- Service-to-service ----
    django_internal_url: str = "http://django:8000"
    internal_http_timeout_seconds: float = Field(default=5.0, gt=0)

    # ---- Public URL prefixes ----
    public_media_base: str = "/media"
    placeholder_base: str = "/placeholder"

    # ---- Uploads (customer avatars) ----
    media_root: Path = DEFAULT_MEDIA_ROOT
    avatar_max_bytes: int = Field(default=2_000_000, ge=10_000)
    # Serve uploaded avatars at /media/avatars/ from this app. Needed on hosts without nginx
    # (e.g. Render behind Netlify), where FastAPI's disk isn't shared with Django.
    serve_media: bool = False

    log_level: str = "INFO"
    rate_limit_enabled: bool = True

    # ---- Pricing (INR, GST-inclusive) ----
    free_shipping_threshold: Decimal = Decimal("500")
    standard_shipping_fee: Decimal = Decimal("49")
    express_shipping_fee: Decimal = Decimal("149")

    # ---- Loyalty: PureRoots Coins (1 coin = ₹1; see services/loyalty_rules.py) ----
    loyalty_enabled: bool = True
    loyalty_earn_percent: int = Field(default=30, ge=0, le=100)
    loyalty_redeem_step_amount: Decimal = Field(default=Decimal("1000"), gt=0)
    loyalty_redeem_max_steps: int = Field(default=3, ge=1, le=20)

    # ---- Google Sheets ----
    google_sheets_enabled: bool = False
    google_sheets_spreadsheet_id: str = ""
    google_sheets_worksheet: str = "Enquiries"
    google_service_account_email: str = ""
    google_private_key: str = ""

    # ---- Email ----
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "PureRoots <hello@pureroot.example>"

    # ---- Payments: Razorpay (enabled only when the key id AND secret are set) ----
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    payment_pending_expiry_minutes: int = Field(default=30, ge=5)
    # 0 disables the in-process sweep (use ``python -m app.scripts.expire_unpaid_orders`` instead).
    payment_expiry_sweep_seconds: int = Field(default=300, ge=0)

    # ---- Payments: direct UPI to the merchant's own UPI ID (enabled only when UPI_VPA is valid) ----
    upi_vpa: str = ""
    upi_payee_name: str = "PureRoots Aahar"
    upi_payment_expiry_minutes: int = Field(default=60, ge=5)

    @field_validator("database_url", mode="before")
    @classmethod
    def _use_psycopg_driver(cls, value: object) -> object:
        """Accept plain ``postgres://`` / ``postgresql://`` URLs (as Render and Heroku provide)."""
        if isinstance(value, str):
            for prefix in ("postgres://", "postgresql://"):
                if value.startswith(prefix):
                    return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @field_validator("google_private_key", mode="before")
    @classmethod
    def _normalise_private_key(cls, value: object) -> object:
        """Strip wrapping quotes and turn literal ``\\n`` sequences into real newlines."""
        if not isinstance(value, str):
            return value
        key = value.strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in {'"', "'"}:
            key = key[1:-1]
        return key.replace("\\n", "\n")

    @field_validator("jwt_algorithm")
    @classmethod
    def _check_algorithm(cls, value: str) -> str:
        algorithm = value.strip().upper()
        if algorithm not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(f"JWT_ALGORITHM must be one of {sorted(_ALLOWED_JWT_ALGORITHMS)}")
        return algorithm

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @model_validator(mode="after")
    def _warn_on_placeholder_secrets(self) -> "Settings":
        if self.is_production:
            for name in ("jwt_secret_key", "internal_api_token"):
                if getattr(self, name).startswith("change-me"):
                    logger.warning("%s still uses the placeholder value from .env.example", name.upper())
            if not self.cookie_secure:
                logger.warning("COOKIE_SECURE is false in production; cookies will be sent over HTTP")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id.strip() and self.razorpay_key_secret.get_secret_value().strip())

    @property
    def upi_vpa_clean(self) -> str:
        return self.upi_vpa.strip()

    @property
    def upi_configured(self) -> bool:
        """Direct UPI is offered only when ``UPI_VPA`` looks like a real UPI ID (``name@bank``)."""
        return bool(UPI_VPA_PATTERN.fullmatch(self.upi_vpa_clean))

    @property
    def google_sheets_configured(self) -> bool:
        return bool(
            self.google_sheets_spreadsheet_id
            and self.google_service_account_email
            and "PRIVATE KEY" in self.google_private_key
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()  # type: ignore[call-arg]
