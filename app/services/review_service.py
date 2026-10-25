"""Product reviews: listing with a rating summary, and one review per customer per product."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.review import Review
from app.models.user import User
from app.repositories import order_repository, review_repository
from app.schemas.review import ReviewCreate, ReviewOut, ReviewSummary
from app.services.catalog_service import get_product_row

DUPLICATE_REVIEW = "You have already reviewed this product."


def author_display_name(full_name: str | None) -> str:
    """``"Asha Kulkarni"`` → ``"Asha K."``."""
    parts = (full_name or "").split()
    if not parts:
        return "PureRoots customer"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


def to_out(review: Review) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        rating=review.rating,
        title=review.title or "",
        body=review.body or "",
        author_name=author_display_name(review.user.full_name if review.user else None),
        is_verified_purchase=review.is_verified_purchase,
        created_at=review.created_at,
    )


def summary_for(distribution: dict[int, int]) -> ReviewSummary:
    full = {stars: distribution.get(stars, 0) for stars in (5, 4, 3, 2, 1)}
    count = sum(full.values())
    avg = round(sum(stars * n for stars, n in full.items()) / count, 1) if count else 0.0
    return ReviewSummary(avg=avg, count=count, distribution=full)


def list_reviews(db: Session, slug_or_id: str, offset: int, limit: int) -> tuple[list[ReviewOut], int, ReviewSummary]:
    product = get_product_row(db, slug_or_id).product
    reviews, total = review_repository.list_approved(db, product.id, offset, limit)
    summary = summary_for(review_repository.rating_distribution(db, product.id))
    return [to_out(review) for review in reviews], total, summary


def create_review(db: Session, user: User, slug_or_id: str, payload: ReviewCreate) -> ReviewOut:
    product = get_product_row(db, slug_or_id).product
    if review_repository.exists_for_user(db, product.id, user.id):
        raise ConflictError(DUPLICATE_REVIEW)
    review = Review(
        product_id=product.id,
        user_id=user.id,
        rating=payload.rating,
        title=payload.title,
        body=payload.body,
        is_approved=True,
        is_verified_purchase=order_repository.has_delivered_purchase(db, user.id, product.id),
    )
    db.add(review)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(DUPLICATE_REVIEW) from exc
    db.refresh(review)
    return to_out(review)
