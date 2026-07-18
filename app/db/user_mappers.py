from __future__ import annotations

from datetime import date, datetime
import sqlite3

from app.models.schemas import (
    AdviceHistoryItem,
    AdviceTimelineItem,
    AlertEventItem,
    AlertRuleItem,
    StockNoteItem,
    WatchlistItem,
)
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
_WATCHLIST_RESEARCH_STATUSES = frozenset({"to_research", "watching", "holding_research", "excluded"})
_WATCHLIST_PRIORITIES = frozenset({"high", "medium", "low"})
_KLINE_ADJUSTMENT_MODES = frozenset({"qfq", "hfq", "none", "unknown"})
_LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def row_to_watchlist_item(row: sqlite3.Row) -> WatchlistItem:
    return WatchlistItem(
        symbol=_clean_text_or_default(_row_value(row, "symbol"), "", 20),
        code=_clean_text_or_default(_row_value(row, "code"), "", 20),
        market=_clean_text_or_default(_row_value(row, "market"), "", 8),
        name=_clean_text_or_default(_row_value(row, "name"), "未知股票", 80),
        note=_clean_optional_text(_row_value(row, "note"), 80),
        group_name=_clean_text_or_default(_row_value(row, "group_name"), "默认", 20),
        pinned=_bounded_int_or_default(_row_value(row, "pinned"), 0, 1, 0) == 1,
        research_status=_watchlist_choice(
            _row_value(row, "research_status"),
            allowed=_WATCHLIST_RESEARCH_STATUSES,
            default="watching",
        ),
        priority=_watchlist_choice(
            _row_value(row, "priority"),
            allowed=_WATCHLIST_PRIORITIES,
            default="medium",
        ),
        next_review_date=_iso_date_or_none(_row_value(row, "next_review_date")),
        last_viewed_at=_local_time_or_none(_row_value(row, "last_viewed_at")),
        unread_change_count=_non_negative_int_or_default(_row_value(row, "unread_change_count")),
        latest_price=_finite_float_or_none(_row_value(row, "latest_price")),
        latest_change_pct=_finite_float_or_none(_row_value(row, "latest_change_pct")),
        latest_source=_clean_optional_text(_row_value(row, "latest_source"), 80),
        latest_at=_clean_optional_text(_row_value(row, "latest_at"), 40),
        created_at=_clean_text_or_default(_row_value(row, "created_at"), "", 40),
        updated_at=_clean_text_or_default(_row_value(row, "updated_at"), "", 40),
    )


def _row_value(row: sqlite3.Row, column: str, default: object = None) -> object:
    try:
        return row[column]
    except (IndexError, KeyError, TypeError):
        return default


def _watchlist_choice(value: object, *, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _iso_date_or_none(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def _local_time_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, _LOCAL_TIME_FORMAT)
    except ValueError:
        return None
    return parsed.strftime(_LOCAL_TIME_FORMAT) if parsed.strftime(_LOCAL_TIME_FORMAT) == text else None


def row_to_advice(row: sqlite3.Row) -> AdviceHistoryItem:
    return AdviceHistoryItem(
        id=_int_or_default(_row_value(row, "id")),
        symbol=_clean_text_or_default(_row_value(row, "symbol"), "", 20),
        code=_clean_text_or_default(_row_value(row, "code"), "", 20),
        market=_clean_text_or_default(_row_value(row, "market"), "", 8),
        name=_clean_text_or_default(_row_value(row, "name"), "未知股票", 80),
        action=_clean_text_or_default(_row_value(row, "action"), "控制风险", 20),
        confidence=_bounded_int(_row_value(row, "confidence"), 0, 100),
        trend_score=_bounded_int(_row_value(row, "trend_score"), 0, 100),
        trend_label=_clean_text_or_default(_row_value(row, "trend_label"), "未知", 20),
        risk_level=_clean_text_or_default(_row_value(row, "risk_level"), "未知", 20),
        price=_finite_float_or_default(_row_value(row, "price")),
        change_pct=_finite_float_or_default(_row_value(row, "change_pct")),
        support=_finite_float_or_default(_row_value(row, "support")),
        resistance=_finite_float_or_default(_row_value(row, "resistance")),
        data_quality_score=_bounded_int(_row_value(row, "data_quality_score"), 0, 100),
        data_quality_level=_clean_text_or_default(_row_value(row, "data_quality_level"), "未知", 20),
        reason=_clean_text_or_default(
            _row_value(row, "reason"),
            "历史记录字段异常，已使用兜底展示。",
            240,
        ),
        summary=_clean_text_or_default(
            _row_value(row, "summary"),
            "历史记录字段异常，已使用兜底展示。",
            500,
        ),
        created_at=_clean_text_or_default(_row_value(row, "created_at"), "", 20),
        updated_at=_clean_optional_text(_row_value(row, "updated_at"), 20),
        repeat_count=_positive_int_or_default(_row_value(row, "repeat_count"), 1),
        kline_adjustment_mode=_kline_adjustment_mode(_row_value(row, "kline_adjustment_mode")),
        kline_anchor_date=_clean_optional_text(_row_value(row, "kline_anchor_date"), 10),
        kline_anchor_close=_finite_float_or_none(_row_value(row, "kline_anchor_close")),
        kline_data_version=_comparison_text_or_default(_row_value(row, "kline_data_version"), "unknown"),
        kline_contract_version=_comparison_text_or_default(_row_value(row, "kline_contract_version"), "unknown"),
    )


def row_to_advice_timeline(row: sqlite3.Row) -> AdviceTimelineItem:
    return AdviceTimelineItem(
        id=_int_or_default(_row_value(row, "id")),
        symbol=_clean_text_or_default(_row_value(row, "symbol"), "", 20),
        code=_clean_text_or_default(_row_value(row, "code"), "", 20),
        market=_clean_text_or_default(_row_value(row, "market"), "", 8),
        name=_clean_text_or_default(_row_value(row, "name"), "未知股票", 80),
        action=_comparison_text_or_none(_row_value(row, "action")),
        confidence=_bounded_int_or_none(_row_value(row, "confidence"), 0, 100),
        trend_score=_bounded_int_or_none(_row_value(row, "trend_score"), 0, 100),
        trend_label=_comparison_text_or_none(_row_value(row, "trend_label")),
        risk_level=_comparison_text_or_none(_row_value(row, "risk_level")),
        price=_finite_float_or_none(_row_value(row, "price")),
        change_pct=_finite_float_or_none(_row_value(row, "change_pct")),
        support=_finite_float_or_none(_row_value(row, "support")),
        resistance=_finite_float_or_none(_row_value(row, "resistance")),
        data_quality_score=_bounded_int_or_none(_row_value(row, "data_quality_score"), 0, 100),
        data_quality_level=_comparison_text_or_none(_row_value(row, "data_quality_level")),
        data_quality_source=_comparison_text_or_none(_row_value(row, "data_quality_source")),
        reason=_comparison_text_or_none(_row_value(row, "reason")),
        summary=_comparison_text_or_none(_row_value(row, "summary")),
        created_at=_clean_text_or_default(_row_value(row, "created_at"), "", 40),
        updated_at=_clean_optional_text(_row_value(row, "updated_at"), 40),
        repeat_count=_positive_int_or_default(_row_value(row, "repeat_count"), 1),
        snapshot_contract_version=_comparison_text_or_default(
            _row_value(row, "snapshot_contract_version"),
            "legacy",
        ),
        conclusion_basis=_comparison_text_or_default(
            _row_value(row, "conclusion_basis"),
            "legacy_unknown",
        ),
        rule_version=_comparison_text_or_default(_row_value(row, "rule_version"), "unknown"),
        model_version=_comparison_text_or_default(_row_value(row, "model_version"), "unknown"),
        market_time=_comparison_text_or_none(_row_value(row, "market_time")),
        kline_adjustment_mode=_kline_adjustment_mode(_row_value(row, "kline_adjustment_mode")),
        kline_anchor_date=_clean_optional_text(_row_value(row, "kline_anchor_date"), 10),
        kline_anchor_close=_finite_float_or_none(_row_value(row, "kline_anchor_close")),
        kline_data_version=_comparison_text_or_default(_row_value(row, "kline_data_version"), "unknown"),
        kline_contract_version=_comparison_text_or_default(_row_value(row, "kline_contract_version"), "unknown"),
    )


def _kline_adjustment_mode(value: object) -> str:
    normalized = str(value or "unknown").strip().lower()
    return normalized if normalized in _KLINE_ADJUSTMENT_MODES else "unknown"


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


def _bounded_int_or_none(value: object, low: int, high: int) -> int | None:
    parsed = finite_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    parsed_int = int(parsed)
    return parsed_int if low <= parsed_int <= high else None


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


def _comparison_text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _comparison_text_or_default(value: object, default: str) -> str:
    return _comparison_text_or_none(value) or default


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
    "row_to_advice_timeline",
    "row_to_alert_rule",
    "row_to_alert_event",
    "row_to_stock_note",
    "alert_condition_label",
]
