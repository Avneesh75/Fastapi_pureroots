"""Contact form and newsletter models."""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import AppSchema, Email, IndianPhone, PersonName, Trimmed


class ContactCreate(AppSchema):
    full_name: PersonName
    email: Email
    mobile: IndianPhone
    subject: Annotated[str, Trimmed, Field(min_length=2, max_length=150)]
    message: Annotated[str, Trimmed, Field(min_length=10, max_length=3000)]
    product_interest: Annotated[str, Trimmed, Field(max_length=200)] | None = None
    city: Annotated[str, Trimmed, Field(max_length=100)] | None = None


class ContactOut(BaseModel):
    id: uuid.UUID
    created_at: datetime


class NewsletterSubscribe(AppSchema):
    email: Email
    source: Annotated[str, Trimmed, Field(max_length=50)] | None = None


class NewsletterOut(BaseModel):
    email: str
    subscribed: bool = True
