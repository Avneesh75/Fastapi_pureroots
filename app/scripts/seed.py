"""Idempotent demo data for FastAPI-owned tables.

Creates ~8 demo customers (password ``PureRoot@123``) with addresses, ~15 historical orders
in varied statuses (with status history and COD payments) and ~40 reviews, using the
catalog that Django has already seeded (read through the read-only ``CatalogBase`` models).

This is historical demo data: it never calls Django's inventory API, so stock levels are
not affected. Safe to run repeatedly::

    python -m app.scripts.seed
"""
import logging
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app import database
from app.config import get_settings
from app.core.clock import utcnow
from app.core.security import hash_password
from app.logging_config import configure_logging
from app.models.catalog import Product, ProductVariant
from app.models.order import Order, OrderItem, OrderStatusHistory, Payment
from app.models.review import Review
from app.models.user import Address, User
from app.repositories import order_repository
from app.services.order_service import ORDER_NUMBER_ALPHABET
from app.services.pricing_service import PriceLine, ShippingRates, price_cart, q2

logger = logging.getLogger("app.scripts.seed")

DEMO_PASSWORD = "PureRoot@123"
DEMO_EMAIL_DOMAIN = "pureroot.in"


@dataclass(frozen=True)
class DemoAddress:
    label: str
    line1: str
    line2: str
    landmark: str
    city: str
    state: str
    pincode: str


@dataclass(frozen=True)
class DemoCustomer:
    full_name: str
    email_local: str
    phone: str
    addresses: tuple[DemoAddress, ...]

    @property
    def email(self) -> str:
        return f"{self.email_local}@{DEMO_EMAIL_DOMAIN}"


CUSTOMERS: tuple[DemoCustomer, ...] = (
    DemoCustomer(
        "Aarav Sharma",
        "aarav.sharma",
        "9810012345",
        (
            DemoAddress("Home", "B-42, Vasant Vihar", "Street 7", "Near Priya Cinema", "New Delhi", "Delhi", "110057"),
            DemoAddress("Office", "DLF Cyber City, Tower 10", "12th Floor", "", "Gurugram", "Haryana", "122002"),
        ),
    ),
    DemoCustomer(
        "Ananya Iyer",
        "ananya.iyer",
        "9840023456",
        (DemoAddress("Home", "14, 3rd Main Road", "Besant Nagar", "Opp. Elliot's Beach", "Chennai", "Tamil Nadu", "600090"),),
    ),
    DemoCustomer(
        "Rohan Mehta",
        "rohan.mehta",
        "9820034567",
        (
            DemoAddress("Home", "701, Sea Breeze Apartments", "Carter Road, Bandra West", "", "Mumbai", "Maharashtra", "400050"),
        ),
    ),
    DemoCustomer(
        "Priya Nair",
        "priya.nair",
        "9895045678",
        (DemoAddress("Home", "TC 9/1123, Sasthamangalam", "", "Near Pipeline Road", "Thiruvananthapuram", "Kerala", "695010"),),
    ),
    DemoCustomer(
        "Kabir Singh",
        "kabir.singh",
        "9876056789",
        (
            DemoAddress("Home", "House 221, Sector 21-C", "", "Near Gurudwara", "Chandigarh", "Chandigarh", "160022"),
            DemoAddress("Parents", "56, Model Town", "Phase 2", "", "Ludhiana", "Punjab", "141002"),
        ),
    ),
    DemoCustomer(
        "Meera Reddy",
        "meera.reddy",
        "9849067890",
        (DemoAddress("Home", "Plot 18, Road No. 12", "Banjara Hills", "", "Hyderabad", "Telangana", "500034"),),
    ),
    DemoCustomer(
        "Arjun Kulkarni",
        "arjun.kulkarni",
        "9822078901",
        (DemoAddress("Home", "Flat 3B, Shanti Kunj", "Kothrud", "Behind City Pride", "Pune", "Maharashtra", "411038"),),
    ),
    DemoCustomer(
        "Ishita Banerjee",
        "ishita.banerjee",
        "9830089012",
        (DemoAddress("Home", "27/1 Lake Place", "Ballygunge", "", "Kolkata", "West Bengal", "700029"),),
    ),
)

# (customer index, days ago, final status, delivery method, number of lines)
ORDER_PLAN: tuple[tuple[int, int, str, str, int], ...] = (
    (0, 58, "delivered", "standard", 3),
    (1, 54, "delivered", "express", 2),
    (2, 49, "refunded", "standard", 2),
    (3, 45, "delivered", "standard", 4),
    (4, 40, "cancelled", "standard", 1),
    (5, 36, "delivered", "express", 2),
    (6, 31, "delivered", "standard", 3),
    (7, 27, "delivered", "standard", 2),
    (0, 21, "delivered", "standard", 2),
    (1, 14, "out_for_delivery", "express", 1),
    (2, 9, "shipped", "standard", 3),
    (3, 6, "packed", "standard", 2),
    (4, 4, "processing", "express", 2),
    (5, 2, "confirmed", "standard", 1),
    (6, 1, "cancelled", "standard", 2),
)

MAIN_PATH = ("pending", "confirmed", "processing", "packed", "shipped", "out_for_delivery", "delivered")

REVIEW_TEMPLATES: dict[int, tuple[tuple[str, str], ...]] = {
    5: (
        ("Tastes like home", "The aroma when I opened the pack took me straight back to my grandmother's kitchen."),
        ("Absolutely worth it", "Clean, fresh and you can tell it is not over-processed. Repeat purchase for sure."),
        ("Best I've tried", "We switched our whole family to this. Consistent quality every single order."),
        ("Beautifully packed", "Arrived well sealed and fresh. The quality is genuinely premium."),
    ),
    4: (
        ("Very good quality", "Fresh and flavourful. Slightly pricey, but the quality justifies it."),
        ("Great everyday staple", "Good taste and texture. Delivery took a day longer than expected."),
        ("Happy with this", "Noticeably better than supermarket brands. Would love a bigger pack size."),
    ),
    3: (
        ("Decent", "Good product overall, though I expected a slightly stronger flavour."),
        ("Okay for the price", "Quality is fine. Packaging could be more eco-friendly."),
    ),
    2: (("Not for me", "The product is fine but the taste was not what my family prefers."),),
}
RATING_WEIGHTS = (5, 5, 5, 5, 4, 4, 4, 4, 3, 2)
TARGET_REVIEWS = 40


# --------------------------------------------------------------------------- customers


def seed_customers(db: Session, rng: random.Random) -> list[User]:
    now = utcnow()
    users: list[User] = []
    password_hash: str | None = None
    created = 0
    for index, demo in enumerate(CUSTOMERS):
        user = db.scalar(select(User).where(User.email == demo.email))
        if user is None:
            password_hash = password_hash or hash_password(DEMO_PASSWORD)
            joined = now - timedelta(days=70 - index * 3, hours=rng.randint(0, 12))
            user = User(
                email=demo.email,
                full_name=demo.full_name,
                phone=demo.phone,
                password_hash=password_hash,
                is_active=True,
                is_verified=True,
                created_at=joined,
                updated_at=joined,
            )
            db.add(user)
            db.flush()
            for position, address in enumerate(demo.addresses):
                db.add(
                    Address(
                        user_id=user.id,
                        label=address.label,
                        full_name=demo.full_name,
                        phone=demo.phone,
                        line1=address.line1,
                        line2=address.line2,
                        landmark=address.landmark,
                        city=address.city,
                        state=address.state,
                        pincode=address.pincode,
                        is_default=position == 0,
                        created_at=joined,
                        updated_at=joined,
                    )
                )
            created += 1
        users.append(user)
    db.commit()
    logger.info("Customers: %s created, %s already present", created, len(users) - created)
    return users


# --------------------------------------------------------------------------- orders


def load_catalog_variants(db: Session) -> list[ProductVariant]:
    try:
        stmt = (
            select(ProductVariant)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(ProductVariant.is_active.is_(True), Product.status == "active")
            .options(selectinload(ProductVariant.product))
            .order_by(Product.popularity.desc(), ProductVariant.sort_order)
        )
        return list(db.scalars(stmt))
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Catalog tables are not readable yet (has Django migrated and seeded?)")
        return []


def _order_number(db: Session, placed_at: datetime, rng: random.Random) -> str:
    while True:
        suffix = "".join(rng.choice(ORDER_NUMBER_ALPHABET) for _ in range(5))
        candidate = f"PR-{placed_at:%y%m%d}-{suffix}"
        if not order_repository.order_number_exists(db, candidate):
            return candidate


def _status_path(final_status: str) -> list[str]:
    if final_status == "cancelled":
        return ["pending", "confirmed", "cancelled"]
    if final_status == "refunded":
        return [*MAIN_PATH, "refunded"]
    return list(MAIN_PATH[: MAIN_PATH.index(final_status) + 1])


def _history_note(status: str) -> tuple[str, str]:
    notes = {
        "pending": ("Order placed", "customer"),
        "confirmed": ("Order confirmed", "system"),
        "processing": ("Being prepared at our Nashik warehouse", "admin:operations"),
        "packed": ("Packed in compostable packaging", "admin:operations"),
        "shipped": ("Handed over to courier", "admin:operations"),
        "out_for_delivery": ("Out for delivery", "admin:operations"),
        "delivered": ("Delivered", "admin:operations"),
        "cancelled": ("Cancelled at customer's request", "customer"),
        "refunded": ("Refund processed after quality complaint", "admin:support"),
    }
    return notes[status]


def seed_orders(db: Session, users: list[User], variants: list[ProductVariant], rng: random.Random) -> int:
    demo_emails = [customer.email for customer in CUSTOMERS]
    existing = db.scalar(select(func.count(Order.id)).where(Order.customer_email.in_(demo_emails))) or 0
    if existing:
        logger.info("Orders: %s demo orders already present, skipping", existing)
        return 0

    settings = get_settings()
    rates = ShippingRates(settings.free_shipping_threshold, settings.standard_shipping_fee, settings.express_shipping_fee)
    now = utcnow()
    created = 0
    for customer_index, days_ago, final_status, delivery_method, line_count in ORDER_PLAN:
        user = users[customer_index]
        demo = CUSTOMERS[customer_index]
        address = demo.addresses[0]
        chosen = rng.sample(variants, k=min(line_count, len(variants)))
        quantities = [rng.randint(1, 3) for _ in chosen]
        price_lines = [
            PriceLine(
                key=str(variant.id),
                unit_price=Decimal(variant.price),
                unit_mrp=Decimal(variant.mrp),
                quantity=quantity,
                tax_rate=Decimal(variant.product.tax_rate or 0),
            )
            for variant, quantity in zip(chosen, quantities, strict=True)
        ]
        breakdown = price_cart(price_lines, None, delivery_method, rates)
        placed_at = now - timedelta(days=days_ago, hours=rng.randint(1, 10), minutes=rng.randint(0, 59))
        path = _status_path(final_status)
        payment_status = {"delivered": "paid", "refunded": "refunded"}.get(final_status, "cod_pending")

        order = Order(
            order_number=_order_number(db, placed_at, rng),
            user_id=user.id,
            status=final_status,
            payment_status=payment_status,
            payment_method="cod",
            delivery_method=delivery_method,
            customer_name=demo.full_name,
            customer_email=demo.email,
            customer_phone=demo.phone,
            shipping_address={
                "full_name": demo.full_name,
                "phone": demo.phone,
                "line1": address.line1,
                "line2": address.line2,
                "landmark": address.landmark,
                "city": address.city,
                "state": address.state,
                "pincode": address.pincode,
            },
            subtotal=breakdown.subtotal,
            discount_total=breakdown.coupon_discount,
            shipping_fee=breakdown.shipping,
            tax_total=breakdown.tax_included,
            grand_total=breakdown.total,
            coupon_code=None,
            notes="",
            placed_at=placed_at,
            created_at=placed_at,
            updated_at=placed_at,
        )
        for variant, quantity in zip(chosen, quantities, strict=True):
            order.items.append(
                OrderItem(
                    product_id=variant.product_id,
                    variant_id=variant.id,
                    product_name=variant.product.name,
                    product_slug=variant.product.slug,
                    variant_label=variant.label,
                    sku=variant.sku,
                    unit_mrp=q2(variant.mrp),
                    unit_price=q2(variant.price),
                    quantity=quantity,
                    tax_rate=variant.product.tax_rate or 0,
                    line_total=q2(Decimal(variant.price) * quantity),
                    created_at=placed_at,
                    updated_at=placed_at,
                )
            )
        previous: str | None = None
        for step, status in enumerate(path):
            note, changed_by = _history_note(status)
            offset_hours = 0 if status == "confirmed" else step * 18
            at = min(
                placed_at + timedelta(hours=offset_hours, minutes=step),
                now - timedelta(minutes=len(path) - step),  # never in the future
            )
            order.status_history.append(
                OrderStatusHistory(from_status=previous, to_status=status, note=note, changed_by=changed_by, created_at=at)
            )
            previous = status
        order.payments.append(
            Payment(
                provider="cod",
                provider_reference="",
                amount=breakdown.total,
                currency="INR",
                status={"paid": "paid", "refunded": "refunded"}.get(payment_status, "pending"),
                raw_response={"method": "cod", "seed": True},
                created_at=placed_at,
                updated_at=placed_at,
            )
        )
        db.add(order)
        created += 1
    db.commit()
    logger.info("Orders: %s demo orders created", created)
    return created


# --------------------------------------------------------------------------- reviews


def seed_reviews(db: Session, users: list[User], variants: list[ProductVariant], rng: random.Random) -> int:
    products: list[Product] = []
    seen: set = set()
    for variant in variants:
        if variant.product_id not in seen:
            seen.add(variant.product_id)
            products.append(variant.product)
    if not products:
        return 0

    existing_pairs = {
        (product_id, user_id) for product_id, user_id in db.execute(select(Review.product_id, Review.user_id)).all()
    }
    demo_user_ids = {user.id for user in users}
    already = sum(1 for _, user_id in existing_pairs if user_id in demo_user_ids)
    if already >= TARGET_REVIEWS:
        logger.info("Reviews: %s demo reviews already present, skipping", already)
        return 0

    now = utcnow()
    created = 0
    # Walk every (product, user) pair, spreading reviews across products first.
    pairs = [
        (products[p], users[(p + offset) % len(users)])
        for offset in range(len(users))
        for p in range(len(products))
    ]
    for product, user in pairs:
        if already + created >= TARGET_REVIEWS:
            break
        if (product.id, user.id) in existing_pairs:
            continue
        rating = rng.choice(RATING_WEIGHTS)
        title, body = rng.choice(REVIEW_TEMPLATES[rating])
        written = now - timedelta(days=rng.randint(1, 50), hours=rng.randint(0, 23))
        db.add(
            Review(
                product_id=product.id,
                user_id=user.id,
                rating=rating,
                title=title,
                body=body,
                is_approved=True,
                is_verified_purchase=order_repository.has_delivered_purchase(db, user.id, product.id),
                created_at=written,
                updated_at=written,
            )
        )
        existing_pairs.add((product.id, user.id))
        created += 1
    db.commit()
    logger.info("Reviews: %s created", created)
    return created


# --------------------------------------------------------------------------- entry point


def run(db: Session) -> None:
    rng = random.Random(20260927)
    users = seed_customers(db, rng)
    variants = load_catalog_variants(db)
    if not variants:
        logger.warning("Catalog is empty — skipping demo orders and reviews. Seed the Django catalog, then re-run.")
        return
    seed_orders(db, users, variants, rng)
    seed_reviews(db, users, variants, rng)


def main() -> int:
    configure_logging(get_settings().log_level)
    db = database.SessionLocal()
    try:
        run(db)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Seeding failed")
        return 1
    finally:
        db.close()
    logger.info("Demo customers are ready (e.g. %s); the demo password is documented in the README", CUSTOMERS[0].email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
