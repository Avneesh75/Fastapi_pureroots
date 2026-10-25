"""/api/v1/loyalty — the signed-in customer's PureRoots Coins."""
from typing import Any

from fastapi import APIRouter, Depends

from app.core.pagination import PageParams, page_meta, page_params
from app.core.responses import ApiResponse, envelope, error_responses
from app.dependencies import CurrentUser, DbSession, SettingsDep
from app.schemas.loyalty import LoyaltySummaryOut
from app.services import loyalty_service

router = APIRouter(prefix="/loyalty", tags=["Loyalty"])


@router.get(
    "",
    response_model=ApiResponse[LoyaltySummaryOut],
    summary="My PureRoots Coins",
    description=(
        "Balances (`available` may be negative after spent coins were reversed; `pending` = promised on orders "
        "not delivered yet), `lifetime_earned`, the `rule` and the ledger `transactions` (newest first, paginated "
        "with `page` / `page_size`; meta has the totals)."
    ),
    responses=error_responses(401),
)
def my_coins(
    user: CurrentUser, db: DbSession, settings: SettingsDep, page: PageParams = Depends(page_params)
) -> dict[str, Any]:
    data, total = loyalty_service.summary(db, user, settings, page.offset, page.page_size)
    return envelope(data, meta=page_meta(page.page, page.page_size, total))
