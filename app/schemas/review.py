"""Review models."""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import AppSchema, Trimmed


class ReviewCreate(AppSchema):
    rating: Annotated[int, Field(ge=1, le=5)]
    title: Annotated[str, Trimmed, Field(max_length=120)] = ""
    body: Annotated[str, Trimmed, Field(max_length=3000)] = ""


class ReviewOut(BaseModel):
    id: uuid.UUID
    rating: int
    title: str
    body: str
    author_name: str
    is_verified_purchase: bool
    created_at: datetime


class ReviewSummary(BaseModel):
    avg: float
    count: int
    distribution: dict[int, int]
