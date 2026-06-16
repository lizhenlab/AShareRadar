from __future__ import annotations

import sqlite3

from app.models.schemas import (
    AdviceHistoryItem,
    AlertEventItem,
    AlertRuleItem,
    Kline,
    MinuteKline,
    MonitorEvent,
    ProviderCapabilityStatus,
    PlateItem,
    ProviderStatus,
    Quote,
    StockConceptItem,
    StockInfo,
    StockNoteItem,
    TaskRun,
    WatchlistItem,
)


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


def row_to_provider_status(row: sqlite3.Row) -> ProviderStatus:
    return ProviderStatus(
        name=row["name"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        healthy=bool(row["healthy"]),
        last_success=row["last_success"],
        last_error=row["last_error"],
        latency_ms=row["latency_ms"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        updated_at=row["updated_at"],
    )


def row_to_provider_capability_status(row: sqlite3.Row) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=row["name"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        healthy=bool(row["healthy"]),
        last_success=row["last_success"],
        last_error=row["last_error"],
        latency_ms=row["latency_ms"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        updated_at=row["updated_at"],
    )


def row_to_task_run(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        task_name=row["task_name"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        message=row["message"],
    )


def row_to_monitor_event(row: sqlite3.Row) -> MonitorEvent:
    return MonitorEvent(
        id=row["id"],
        level=row["level"],
        category=row["category"],
        symbol=row["symbol"],
        message=row["message"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        repeat_count=row["repeat_count"] or 1,
    )


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
        id=row["id"],
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        name=row["name"],
        action=row["action"],
        confidence=row["confidence"],
        trend_score=row["trend_score"],
        trend_label=row["trend_label"],
        risk_level=row["risk_level"],
        price=row["price"],
        change_pct=row["change_pct"],
        support=row["support"],
        resistance=row["resistance"],
        data_quality_score=row["data_quality_score"],
        data_quality_level=row["data_quality_level"],
        reason=row["reason"],
        summary=row["summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        repeat_count=row["repeat_count"] or 1,
    )


def row_to_alert_rule(row: sqlite3.Row) -> AlertRuleItem:
    return AlertRuleItem(
        id=row["id"],
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        stock_name=row["stock_name"],
        name=row["name"],
        condition_type=row["condition_type"],
        condition_label=alert_condition_label(row["condition_type"]),
        threshold=row["threshold"],
        note=row["note"],
        enabled=bool(row["enabled"]),
        last_checked_at=row["last_checked_at"],
        last_triggered_at=row["last_triggered_at"],
        last_state=row["last_state"],
        trigger_count=row["trigger_count"] or 0,
        cooldown_seconds=row["cooldown_seconds"] or 300,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_alert_event(row: sqlite3.Row) -> AlertEventItem:
    return AlertEventItem(
        id=row["id"],
        rule_id=row["rule_id"],
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        stock_name=row["stock_name"],
        name=row["name"],
        condition_type=row["condition_type"],
        event_type=row["event_type"],
        message=row["message"],
        price=row["price"],
        change_pct=row["change_pct"],
        threshold=row["threshold"],
        created_at=row["created_at"],
    )


def row_to_stock_note(row: sqlite3.Row) -> StockNoteItem:
    return StockNoteItem(
        id=row["id"],
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        name=row["name"],
        note_type=row["note_type"],
        content=row["content"],
        price=row["price"],
        trade_date=row["trade_date"],
        color=row["color"],
        visible=bool(row["visible"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def alert_condition_label(condition_type: str) -> str:
    labels = {
        "price_above": "价格高于",
        "price_below": "价格低于",
        "change_pct_above": "涨幅高于",
        "change_pct_below": "跌幅低于",
        "trend_score_above": "趋势评分高于",
        "trend_score_below": "趋势评分低于",
        "break_support": "跌破支撑",
        "break_resistance": "突破压力",
    }
    return labels.get(condition_type, condition_type)
