from __future__ import annotations

from app.models.schemas import Kline, Quote, StrongStockItem
from app.services.indicators import recent_volume_ratio, trend_score
from app.services.leader_scoring import (
    STRONG_STOCK_LEADER_PROFILE,
    STRONG_STOCK_TAG_RULES,
    LeaderScoreInput,
    leader_score,
    leader_tags,
)


def build_strong_stock_watch(quotes: list[Quote], kline_map: dict[str, list[Kline]]) -> list[StrongStockItem]:
    items: list[StrongStockItem] = []
    for quote in quotes:
        klines = kline_map.get(quote.code, [])
        if not klines:
            continue
        score, _ = trend_score(quote, klines)
        volume_ratio = recent_volume_ratio(klines)
        leader_score = _strong_stock_leader_score(quote, score, volume_ratio)
        reason = _strength_reason(quote, score)
        items.append(
            StrongStockItem(
                rank=0,
                code=quote.code,
                name=quote.name,
                price=quote.price,
                change_pct=quote.change_pct,
                trend_score=score,
                reason=reason,
                leader_score=leader_score,
                tags=_strong_stock_tags(quote, score, volume_ratio, leader_score),
            )
        )
    ranked = sorted(items, key=lambda item: (item.trend_score, item.change_pct), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ranked


def _strong_stock_leader_score(quote: Quote, trend_score: int, volume_ratio: float) -> int:
    return leader_score(_strong_stock_leader_inputs(quote, trend_score, volume_ratio), STRONG_STOCK_LEADER_PROFILE)


def _strong_stock_tags(quote: Quote, trend_score: int, volume_ratio: float, leader_score: int) -> list[str]:
    return leader_tags(_strong_stock_leader_inputs(quote, trend_score, volume_ratio), leader_score, STRONG_STOCK_TAG_RULES, "观察")


def _strong_stock_leader_inputs(quote: Quote, trend_score: int, volume_ratio: float) -> LeaderScoreInput:
    return LeaderScoreInput(
        trend_score=trend_score,
        change_pct=quote.change_pct,
        volume_ratio=volume_ratio,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
    )


def _strength_reason(quote: Quote, score: int) -> str:
    pieces = []
    if score >= 75:
        pieces.append("趋势评分靠前")
    if quote.change_pct > 0:
        pieces.append(f"今日上涨 {quote.change_pct:.2f}%")
    if quote.turnover_rate:
        pieces.append(f"换手 {quote.turnover_rate:.2f}%")
    if quote.amount:
        pieces.append(f"成交额 {quote.amount / 100000000:.1f} 亿")
    return "，".join(pieces) or "等待更多行情确认"
