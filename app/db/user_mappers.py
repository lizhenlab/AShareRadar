from __future__ import annotations

import sqlite3

from app.models.schemas import AdviceHistoryItem, AlertEventItem, AlertRuleItem, StockNoteItem, WatchlistItem
from app.utils.market_data import finite_float


ALERT_CONDITION_LABELS = {
    "price_above": "价格高于",
    "price_below": "价格低于",
    "change_pct_above": "涨幅高于",
    "change_pct_below": "跌幅低于",
    "trend_score_above": "趋势评分高于",
    "trend_score_below": "趋势评分低于",
    "break_support": "跌破支撑",
    "break_resistance": "突破压力",
}
_SUPPORTED_ALERT_CONDITIONS = frozenset(ALERT_CONDITION_LABELS)


def row_to_watchlist_item(row: sqlite3.Row) -> WatchlistItem:
    return WatchlistItem(
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        name=row["name"],
        note=row["note"],
        group_name=row["group_name"],
        pinned=bool(row["pinned"]),
        latest_price=row["latest_price"],
        latest_change_pct=row["latest_change_pct"],
        latest_source=row["latest_source"],
        latest_at=row["latest_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_advice(row: sqlite3.Row) -> AdviceHistoryItem:
    return AdviceHistoryItem(
        id=_int_or_default(row["id"]),
        symbol=_clean_text_or_default(row["symbol"], "", 20),
        code=_clean_text_or_default(row["code"], "", 20),
        market=_clean_text_or_default(row["market"], "", 8),
        name=_clean_text_or_default(row["name"], "未知股票", 80),
        action=_clean_text_or_default(row["action"], "控制风险", 20),
        confidence=_bounded_int(row["confidence"], 0, 100),
        trend_score=_bounded_int(row["trend_score"], 0, 100),
        trend_label=_clean_text_or_default(row["trend_label"], "未知", 20),
        risk_level=_clean_text_or_default(row["risk_level"], "未知", 20),
        price=_finite_float_or_default(row["price"]),
        change_pct=_finite_float_or_default(row["change_pct"]),
        support=_finite_float_or_default(row["support"]),
        resistance=_finite_float_or_default(row["resistance"]),
        data_quality_score=_bounded_int(row["data_quality_score"], 0, 100),
        data_quality_level=_clean_text_or_default(row["data_quality_level"], "未知", 20),
        reason=_clean_text_or_default(row["reason"], "历史记录字段异常，已使用兜底展示。", 240),
        summary=_clean_text_or_default(row["summary"], "历史记录字段异常，已使用兜底展示。", 500),
        created_at=_clean_text_or_default(row["created_at"], "", 20),
        updated_at=_clean_optional_text(row["updated_at"], 20),
        repeat_count=_positive_int_or_default(row["repeat_count"], 1),
    )


def _finite_float_or_default(value: object, default: float = 0.0) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None else default


def _int_or_default(value: object, default: int = 0) -> int:
    parsed = finite_float(value)
    return int(parsed) if parsed is not None else default


def _non_negative_int_or_default(value: object, default: int = 0) -> int:
    parsed = finite_float(value)
    if parsed is None:
        return default
    return max(0, int(parsed))


def _positive_int_or_default(value: object, default: int) -> int:
    parsed = finite_float(value)
    if parsed is None or parsed <= 0:
        return default
    return int(parsed)


def _bounded_int(value: object, low: int, high: int) -> int:
    parsed = finite_float(value)
    if parsed is None:
        return low
    return max(low, min(high, int(parsed)))


def _bounded_int_or_default(value: object, low: int, high: int, default: int) -> int:
    parsed = finite_float(value)
    if parsed is None:
        return default
    parsed_int = int(parsed)
    if parsed_int < low or parsed_int > high:
        return default
    return parsed_int


def _clean_text_or_default(value: object, default: str, max_length: int) -> str:
    text = str(value or "").strip()
    return (text or default)[:max_length]


def _clean_optional_text(value: object, max_length: int) -> str | None:
    text = str(value or "").strip()
    return text[:max_length] or None


def row_to_alert_rule(row: sqlite3.Row) -> AlertRuleItem:
    threshold = finite_float(row["threshold"])
    condition_type = _clean_text_or_default(row["condition_type"], "unknown", 40)
    fallback_name = f"{alert_condition_label(condition_type)} {_finite_float_or_default(threshold):g}"
    enabled = _bounded_int_or_default(row["enabled"], 0, 1, 0) == 1
    executable = enabled and threshold is not None and _supported_alert_condition(condition_type)
    return AlertRuleItem(
        id=_int_or_default(row["id"]),
        symbol=_clean_text_or_default(row["symbol"], "", 20),
        code=_clean_text_or_default(row["code"], "", 20),
        market=_clean_text_or_default(row["market"], "", 8),
        stock_name=_clean_text_or_default(row["stock_name"], "", 80),
        name=_clean_text_or_default(row["name"], fallback_name, 40),
        condition_type=condition_type,
        condition_label=alert_condition_label(condition_type),
        threshold=threshold if threshold is not None else 0.0,
        note=_clean_optional_text(row["note"], 160),
        enabled=executable,
        last_checked_at=_clean_optional_text(row["last_checked_at"], 20),
        last_triggered_at=_clean_optional_text(row["last_triggered_at"], 20),
        last_state=_clean_text_or_default(row["last_state"], "等待", 20),
        trigger_count=_non_negative_int_or_default(row["trigger_count"]),
        cooldown_seconds=_bounded_int_or_default(row["cooldown_seconds"], 30, 86400, 300),
        created_at=_clean_text_or_default(row["created_at"], "", 20),
        updated_at=_clean_text_or_default(row["updated_at"], "", 20),
    )


def row_to_alert_event(row: sqlite3.Row) -> AlertEventItem:
    condition_type = _clean_text_or_default(row["condition_type"], "unknown", 40)
    return AlertEventItem(
        id=_int_or_default(row["id"]),
        rule_id=_int_or_default(row["rule_id"]),
        symbol=_clean_text_or_default(row["symbol"], "", 20),
        code=_clean_text_or_default(row["code"], "", 20),
        market=_clean_text_or_default(row["market"], "", 8),
        stock_name=_clean_text_or_default(row["stock_name"], "", 80),
        name=_clean_text_or_default(row["name"], alert_condition_label(condition_type), 40),
        condition_type=condition_type,
        event_type=_clean_text_or_default(row["event_type"], "触发", 20),
        message=_clean_text_or_default(row["message"], "预警状态已更新", 240),
        price=_finite_float_or_default(row["price"]),
        change_pct=_finite_float_or_default(row["change_pct"]),
        threshold=_finite_float_or_default(row["threshold"]),
        created_at=_clean_text_or_default(row["created_at"], "", 20),
    )


def row_to_stock_note(row: sqlite3.Row) -> StockNoteItem:
    return StockNoteItem(
        id=_int_or_default(row["id"]),
        symbol=_clean_text_or_default(row["symbol"], "", 20),
        code=_clean_text_or_default(row["code"], "", 20),
        market=_clean_text_or_default(row["market"], "", 8),
        name=_clean_text_or_default(row["name"], "未知股票", 80),
        note_type=_clean_text_or_default(row["note_type"], "观察", 20),
        content=_clean_text_or_default(row["content"], "历史笔记字段异常，已使用兜底展示。", 500),
        price=_finite_float_or_none(row["price"]),
        trade_date=_clean_optional_text(row["trade_date"], 20),
        color=_clean_optional_text(row["color"], 20),
        visible=_bounded_int_or_default(row["visible"], 0, 1, 0) == 1,
        created_at=_clean_text_or_default(row["created_at"], "", 20),
        updated_at=_clean_text_or_default(row["updated_at"], "", 20),
    )


def _finite_float_or_none(value: object) -> float | None:
    return finite_float(value)


def alert_condition_label(condition_type: str) -> str:
    return ALERT_CONDITION_LABELS.get(condition_type, condition_type)


def _supported_alert_condition(condition_type: str) -> bool:
    return condition_type in _SUPPORTED_ALERT_CONDITIONS


__all__ = [
    "ALERT_CONDITION_LABELS",
    "row_to_watchlist_item",
    "row_to_advice",
    "row_to_alert_rule",
    "row_to_alert_event",
    "row_to_stock_note",
    "alert_condition_label",
]
