"""Catalog response models (ProductCard, ProductDetail, facets, suggestions)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    short_description: str
    description: str
    image_url: str
    accent_color: str
    is_featured: bool
    sort_order: int
    product_count: int
    seo_title: str
    seo_description: str


class OfferOut(BaseModel):
    """The offer applied to a variant or cart line (OFFERS_AND_ADMIN §3)."""

    id: uuid.UUID
    name: str = Field(examples=["Spice Festival"])
    label: str = Field(examples=["20% OFF"])
    ends_at: datetime | None


class VariantOut(BaseModel):
    id: uuid.UUID
    sku: str
    label: str
    weight_grams: int
    mrp: float
    price: float = Field(description="Selling price (before offers).")
    discount_percent: int = Field(description="Discount of the selling price off MRP.")
    in_stock: bool
    available_quantity: int
    final_price: float = Field(examples=[399.0], description="Selling price minus the best live offer.")
    offer_discount: float = Field(examples=[100.0], description="Per-unit offer discount (0 when none).")
    offer_discount_percent: int = Field(examples=[20], description="round(offer_discount / price * 100).")
    offer: OfferOut | None = None


class ProductCard(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    short_description: str
    category: CategoryRef
    image_url: str
    rating_avg: float = Field(examples=[4.6])
    rating_count: int = Field(examples=[12])
    tags: list[str]
    is_featured: bool
    is_bestseller: bool
    in_stock: bool
    price_from: float = Field(examples=[199.0])
    mrp_from: float = Field(examples=[240.0])
    max_discount_percent: int = Field(examples=[17])
    default_variant_id: uuid.UUID | None
    variants: list[VariantOut]
    final_price_from: float = Field(
        examples=[159.2], description="Min final_price over in-stock variants (all variants if none in stock)."
    )
    offer_label: str | None = Field(None, examples=["20% OFF"], description="Default variant's offer label.")
    offer_hints: list[str] = Field(
        default_factory=list, examples=[["Extra 10% OFF on orders above ₹1,499"]]
    )


class NutritionFact(BaseModel):
    label: str
    value: str


class ProductImageOut(BaseModel):
    url: str
    alt: str


class ProductDetail(ProductCard):
    description: str
    key_benefits: list[str]
    ingredients: str
    nutrition: list[NutritionFact]
    storage_info: str
    shelf_life: str
    how_to_use: str
    origin: str
    certifications: str
    images: list[ProductImageOut]
    tax_rate: float
    seo_title: str
    seo_description: str


class CategoryFacet(BaseModel):
    slug: str
    name: str
    count: int


class PriceRange(BaseModel):
    min: float
    max: float


class ProductFiltersOut(BaseModel):
    categories: list[CategoryFacet]
    price: PriceRange
    weights: list[str]
    ratings: list[int] = Field(default_factory=lambda: [4, 3, 2, 1])


class SuggestedProduct(BaseModel):
    name: str
    slug: str
    image_url: str
    price_from: float


class SuggestedCategory(BaseModel):
    name: str
    slug: str


class SuggestionsOut(BaseModel):
    products: list[SuggestedProduct]
    categories: list[SuggestedCategory]
    keywords: list[str]
