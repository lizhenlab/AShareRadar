from __future__ import annotations

import sqlite3

from app.models.schemas import Kline, MinuteKline, PlateItem, Quote, StockConceptItem, StockInfo


def row_to_quote(row: sqlite3.Row) -> Quote:
    return Quote(
        code=row["code"],
        name=row["name"],
        market=row["market"],
        price=row["price"],
        prev_close=row["prev_close"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        volume=row["volume"],
        amount=row["amount"],
        change=row["change"],
        change_pct=row["change_pct"],
        turnover_rate=row["turnover_rate"],
        pe=row["pe"],
        pb=row["pb"],
        market_cap=row["market_cap"],
        timestamp=row["quote_timestamp"],
        source=f"{row['source']}·缓存",
    )


def row_to_kline(row: sqlite3.Row) -> Kline:
    return Kline(
        date=row["date"],
        open=row["open"],
        close=row["close"],
        high=row["high"],
        low=row["low"],
        volume=row["volume"],
        source=row["source"],
        fetched_at=row["fetched_at"],
        from_cache=True,
    )


def row_to_minute_kline(row: sqlite3.Row) -> MinuteKline:
    return MinuteKline(
        timestamp=row["timestamp"],
        open=row["open"],
        close=row["close"],
        high=row["high"],
        low=row["low"],
        volume=row["volume"],
        amount=row["amount"],
        turnover_rate=row["turnover_rate"],
        source=row["source"],
        interval=row["interval"],
        fetched_at=row["fetched_at"],
        from_cache=True,
    )


def row_to_stock_info(row: sqlite3.Row) -> StockInfo:
    return StockInfo(
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        name=row["name"],
        industry=row["industry"],
        list_date=row["list_date"],
        source=row["source"],
        updated_at=row["updated_at"],
    )


def row_to_plate_item(row: sqlite3.Row) -> PlateItem:
    return PlateItem(
        rank=row["rank"],
        name=row["name"],
        change_pct=row["change_pct"],
        amount=row["amount"],
        turnover_rate=row["turnover_rate"],
        leading_stock=row["leading_stock"],
        leading_stock_change_pct=row["leading_stock_change_pct"],
        source=row["source"],
        updated_at=row["updated_at"],
    )


def row_to_stock_concept_item(row: sqlite3.Row) -> StockConceptItem:
    return StockConceptItem(
        symbol=row["symbol"],
        rank=row["rank"],
        name=row["name"],
        change_pct=row["change_pct"],
        amount=row["amount"],
        turnover_rate=row["turnover_rate"],
        leading_stock=row["leading_stock"],
        leading_stock_change_pct=row["leading_stock_change_pct"],
        match_reason=row["match_reason"],
        source=row["source"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "row_to_quote",
    "row_to_kline",
    "row_to_minute_kline",
    "row_to_stock_info",
    "row_to_plate_item",
    "row_to_stock_concept_item",
]
