"""Pagination helpers producing ``{"page","page_size","total","total_pages"}`` meta."""
import math
from dataclasses import dataclass
from typing import Any

from fastapi import Query

from app.core.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


@dataclass(frozen=True)
class PageParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, description=f"Items per page (values above {MAX_PAGE_SIZE} are clamped)."
    ),
) -> PageParams:
    """FastAPI dependency for ``page`` / ``page_size`` query parameters."""
    return PageParams(page=page, page_size=min(page_size, MAX_PAGE_SIZE))


def page_meta(page: int, page_size: int, total: int) -> dict[str, Any]:
    """Meta block for paginated responses."""
    total_pages = math.ceil(total / page_size) if total and page_size else 0
    return {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages}
