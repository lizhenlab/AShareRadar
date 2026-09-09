from __future__ import annotations

import sqlite3
from typing import TypedDict, cast

from app.models.market import (
    Kline,
    KlineCorporateActionStatus,
    KlineOpenExecutionStatus,
    KlineSessionStatus,
    MinuteKline,
    PlateItem,
    Quote,
    StockConceptItem,
    StockInfo,
)
from app.utils.market_data import finite_float


class KlineExecutionMetadata(TypedDict):
    session_status: KlineSessionStatus
    open_execution_status: KlineOpenExecutionStatus
    corporate_action_status: KlineCorporateActionStatus
    adjustment_factor: float | None
    point_in_time: bool
    execution_metadata_version: str | None


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
        from_cache=True,
        fallback_used=bool(row["fallback_used"]),
    )


def row_to_kline(row: sqlite3.Row) -> Kline:
    return Kline(
        date=row["date"],
        open=row["open"],
        close=row["close"],
        high=row["high"],
        low=row["low"],
        volume=row["volume"],
        **kline_execution_metadata_from_row(row),
        adjustment_mode=row["adjustment_mode"],
        as_of=row["as_of"],
        data_version=row["data_version"],
        contract_version=row["contract_version"],
        source=row["source"],
        fetched_at=row["fetched_at"],
        from_cache=True,
        fallback_used=bool(row["fallback_used"]),
    )


def kline_execution_metadata_from_row(row: sqlite3.Row) -> KlineExecutionMetadata:
    """Preserve raw evidence; missing legacy columns never establish authority."""
    defaults: dict[str, object] = {
        "session_status": "unknown", "open_execution_status": "unknown", "corporate_action_status": "unknown",
        "adjustment_factor": None, "point_in_time": 0, "execution_metadata_version": None,
    }
    columns = set(row.keys())
    values = {name: row[name] if name in columns else default for name, default in defaults.items()}
    _validate_kline_execution_metadata(values)
    values["point_in_time"] = values["point_in_time"] == 1
    values["adjustment_factor"] = finite_float(values["adjustment_factor"])
    return cast(KlineExecutionMetadata, values)


def _validate_kline_execution_metadata(values: dict[str, object]) -> None:
    choices = {
        "session_status": ("trading", "suspended", "unknown"),
        "open_execution_status": ("tradable", "locked_limit_up", "locked_limit_down", "unavailable", "unknown"),
        "corporate_action_status": ("none", "effective_event", "unknown"),
    }
    for name, allowed in choices.items():
        if type(values[name]) is not str or values[name] not in allowed:
            raise ValueError(f"日K {name} 原始类型或取值无效")
    if type(values["point_in_time"]) is not int or values["point_in_time"] not in (0, 1):
        raise ValueError("日K point_in_time 必须是 SQLite 0/1")
    factor = values["adjustment_factor"]
    if factor is not None and (type(factor) not in (int, float) or finite_float(factor) is None):
        raise ValueError("日K adjustment_factor 必须是有限数值或空值")
    version = values["execution_metadata_version"]
    if version is not None and type(version) is not str:
        raise ValueError("日K execution_metadata_version 必须是字符串或空值")


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
        fallback_used=bool(row["fallback_used"]),
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
    "KlineExecutionMetadata",
    "kline_execution_metadata_from_row",
    "row_to_quote",
    "row_to_kline",
    "row_to_minute_kline",
    "row_to_stock_info",
    "row_to_plate_item",
    "row_to_stock_concept_item",
]
