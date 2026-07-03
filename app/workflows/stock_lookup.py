from __future__ import annotations

from app.models.schemas import PlateItem, StockInfo
from app.services.datahub import DataHub
from app.utils.errors import NotFoundError


async def confirmed_stock_profile(datahub: DataHub, symbol: str) -> StockInfo | None:
    try:
        profile = await datahub.stock_profile(symbol)
    except RuntimeError as exc:
        raise RuntimeError(f"股票池暂不可用，无法确认股票代码：{symbol}；{exc}") from exc
    if profile is None:
        raise NotFoundError(f"股票代码不存在或当前股票池不支持：{symbol}")
    return profile


def match_industry(profile: StockInfo | None, plates: list[PlateItem]) -> PlateItem | None:
    if not profile or not profile.industry:
        return None
    for item in plates:
        if item.name == profile.industry:
            return item
    return None


__all__ = ["confirmed_stock_profile", "match_industry"]
