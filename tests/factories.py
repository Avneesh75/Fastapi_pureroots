"""Factory helpers. Catalog rows are inserted directly into SQLite through the read-only
``CatalogBase`` models — something only tests ever do."""
import itertools
import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.catalog import (
    Category,
    Coupon,
    Offer,
    Product,
    ProductImage,
    ProductVariant,
    StockLevel,
    offers_categories,
    offers_products,
)
from app.models.review import Review
from app.models.user import User

_counter = itertools.count(1)

DEFAULT_PASSWORD = "Secret123"


def _next() -> int:
    return next(_counter)


def create_category(db: Session, name: str = "Atta & Flours", slug: str | None = None, **kwargs: Any) -> Category:
    category = Category(name=name, slug=slug or f"category-{_next()}", **kwargs)
    db.add(category)
    db.commit()
    return category


def create_product(
    db: Session,
    category: Category,
    name: str = "Stone-Ground Whole Wheat Atta",
    slug: str | None = None,
    **kwargs: Any,
) -> Product:
    kwargs.setdefault("tax_rate", Decimal("5.00"))
    kwargs.setdefault("status", "active")
    kwargs.setdefault("tags", [])
    kwargs.setdefault("nutrition", [])
    product = Product(category_id=category.id, name=name, slug=slug or f"product-{_next()}", **kwargs)
    db.add(product)
    db.commit()
    return product


def create_variant(
    db: Session,
    product: Product,
    label: str = "1 kg",
    price: str | Decimal = "199.00",
    mrp: str | Decimal = "240.00",
    weight_grams: int = 1000,
    stock: int | None = 50,
    reserved: int = 0,
    **kwargs: Any,
) -> ProductVariant:
    variant = ProductVariant(
        product_id=product.id,
        sku=kwargs.pop("sku", f"PR-TEST-{_next():03d}-{label.replace(' ', '').upper()}"),
        label=label,
        weight_grams=weight_grams,
        price=Decimal(str(price)),
        mrp=Decimal(str(mrp)),
        cost_price=Decimal("100.00"),
        **kwargs,
    )
    db.add(variant)
    db.flush()
    if stock is not None:
        db.add(StockLevel(variant_id=variant.id, warehouse_id=uuid.uuid4(), on_hand=stock, reserved=reserved))
    db.commit()
    return variant


def set_stock(db: Session, variant: ProductVariant, on_hand: int, reserved: int = 0) -> None:
    for level in db.query(StockLevel).filter(StockLevel.variant_id == variant.id):
        level.on_hand = on_hand
        level.reserved = reserved
    db.commit()


def create_image(db: Session, product: Product, path: str = "products/atta.jpg", sort_order: int = 0) -> ProductImage:
    image = ProductImage(product_id=product.id, image=path, alt_text=product.name, sort_order=sort_order)
    db.add(image)
    db.commit()
    return image


def create_coupon(
    db: Session,
    code: str = "SAVE10",
    discount_type: str = "percent",
    value: str = "10",
    min_order_value: str = "0",
    max_discount: str | None = None,
    **kwargs: Any,
) -> Coupon:
    coupon = Coupon(
        code=code.upper(),
        discount_type=discount_type,
        value=Decimal(value),
        min_order_value=Decimal(min_order_value),
        max_discount=Decimal(max_discount) if max_discount is not None else None,
        **kwargs,
    )
    db.add(coupon)
    db.commit()
    return coupon


def create_offer(
    db: Session,
    name: str = "Spice Festival",
    discount_type: str = "percent",
    value: str = "20",
    scope: str = "all",
    categories: Iterable[Category] = (),
    products: Iterable[Product] = (),
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    min_order_amount: str | None = None,
    max_discount_amount: str | None = None,
    priority: int = 0,
    is_active: bool = True,
) -> Offer:
    """Insert an ``offers`` row (+ M2M rows). Datetimes should be UTC (SQLite stores them naive)."""
    offer = Offer(
        name=name,
        discount_type=discount_type,
        value=Decimal(value),
        scope=scope,
        starts_at=starts_at,
        ends_at=ends_at,
        min_order_amount=Decimal(min_order_amount) if min_order_amount is not None else None,
        max_discount_amount=Decimal(max_discount_amount) if max_discount_amount is not None else None,
        priority=priority,
        is_active=is_active,
    )
    db.add(offer)
    db.flush()
    for category in categories:
        db.execute(insert(offers_categories).values(offer_id=offer.id, category_id=category.id))
    for product in products:
        db.execute(insert(offers_products).values(offer_id=offer.id, product_id=product.id))
    db.commit()
    return offer


def create_user(
    db: Session,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
    full_name: str = "Asha Kulkarni",
    phone: str | None = "9876543210",
) -> User:
    user = User(
        email=(email or f"user{_next()}@testmail.in").lower(),
        full_name=full_name,
        phone=phone,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    return user


def create_review(db: Session, product: Product, user: User, rating: int = 5, **kwargs: Any) -> Review:
    review = Review(product_id=product.id, user_id=user.id, rating=rating, title="Great", body="Lovely", **kwargs)
    db.add(review)
    db.commit()
    return review


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.token_version)}"}


def shipping_address(**overrides: Any) -> dict[str, Any]:
    address = {
        "full_name": "Asha Kulkarni",
        "phone": "9876543210",
        "line1": "12, MG Road",
        "line2": "Indiranagar",
        "landmark": "Near Metro",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560038",
    }
    address.update(overrides)
    return address


def checkout_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "customer": {"full_name": "Asha Kulkarni", "email": "asha@testmail.in", "phone": "9876543210"},
        "shipping_address": shipping_address(),
        "address_id": None,
        "save_address": False,
        "delivery_method": "standard",
        "payment_method": "cod",
        "notes": "",
    }
    payload.update(overrides)
    return payload
