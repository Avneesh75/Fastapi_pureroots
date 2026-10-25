"""Shared constants from the architecture contract (ARCHITECTURE.md §4-5)."""

API_PREFIX = "/api/v1"

# ---- Cookies & headers (§5) ----
ACCESS_COOKIE = "pr_access"
REFRESH_COOKIE = "pr_refresh"
CART_COOKIE = "pr_cart"
REFRESH_COOKIE_PATH = "/api/v1/auth"
CART_COOKIE_MAX_AGE = 60 * 24 * 60 * 60  # 60 days
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "PureRoot"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

# ---- Catalog ----
LISTABLE_PRODUCT_STATUSES = ("active", "out_of_stock")
SELLABLE_PRODUCT_STATUS = "active"
PRODUCT_SORTS = ("popularity", "price_asc", "price_desc", "newest", "rating")
MAX_PAGE_SIZE = 60
DEFAULT_PAGE_SIZE = 24

# ---- Cart / checkout ----
MAX_LINE_QUANTITY = 20
DELIVERY_METHODS = ("standard", "express")

# ---- Orders (§4.3) ----
ORDER_STATUSES = (
    "pending",
    "confirmed",
    "processing",
    "packed",
    "shipped",
    "out_for_delivery",
    "delivered",
    "cancelled",
    "refunded",
)
ORDER_STATUS_LABELS = {
    "pending": "Order placed",
    "confirmed": "Confirmed",
    "processing": "Processing",
    "packed": "Packed",
    "shipped": "Shipped",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "refunded": "Refunded",
}
# ``awaiting_verification``: a direct-UPI order whose customer submitted a UTR that staff must confirm.
PAYMENT_STATUSES = ("pending", "cod_pending", "awaiting_verification", "paid", "failed", "refunded")
PAYMENT_STATUS_LABELS = {
    "pending": "Payment pending",
    "cod_pending": "Pay on delivery",
    "awaiting_verification": "Verifying payment",
    "paid": "Paid",
    "failed": "Payment failed",
    "refunded": "Refunded",
}

# ---- Contact ----
CONTACT_STATUSES = ("new", "in_progress", "resolved")
SHEETS_SYNC_STATUSES = ("pending", "synced", "failed", "disabled")

# ---- Indian states and union territories ----
INDIAN_STATES = (
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    # Union territories
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
)
