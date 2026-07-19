from __future__ import annotations

import json
import math
import sqlite3

from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanRun,
    MarketScanSort,
    MarketScanSortOrder,
)


DEGRADATION_DISPLAY_TAGS = frozenset({"兜底行情", "兜底K线", "上市日期未知"})


def run_from_row(row: sqlite3.Row) -> MarketScanRun:
    total = int(row["total_count"] or 0)
    processed = int(row["processed_count"] or 0)
    success = int(row["success_count"] or 0)
    status = str(row["status"])
    progress = 100.0 if total == 0 and status in {"success", "degraded"} else percentage(processed, total)
    return MarketScanRun(
        id=row["id"],
        task_run_id=row["task_run_id"],
        retry_of_run_id=row["retry_of_run_id"],
        status=status,
        trigger=row["trigger"],
        rule_version=row["rule_version"],
        as_of=row["as_of"],
        data_date=row["data_date"],
        scope=row["scope"],
        stock_pool_source=row["stock_pool_source"],
        total_count=total,
        excluded_count=int(row["excluded_count"] or 0),
        processed_count=processed,
        success_count=success,
        missing_count=int(row["missing_count"] or 0),
        skipped_count=int(row["skipped_count"] or 0),
        retry_count=int(row["retry_count"] or 0),
        progress_pct=progress,
        coverage_pct=percentage(success, total),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        message=row["message"],
        last_error=row["last_error"],
        cancel_requested_at=row["cancel_requested_at"],
    )


def result_from_row(row: sqlite3.Row) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=row["run_id"],
        symbol=row["symbol"],
        code=row["code"],
        market=row["market"],
        name=row["name"],
        industry=row["industry"],
        list_date=row["list_date"],
        is_st=bool(row["is_st"]),
        is_new=bool(row["is_new"]),
        metadata_source=row["metadata_source"],
        status=row["status"],
        rank=row["rank"],
        score=row["score"],
        trend_score=row["trend_score"],
        leader_score=row["leader_score"],
        data_quality_score=row["data_quality_score"],
        price=row["price"],
        change_pct=row["change_pct"],
        turnover_rate=row["turnover_rate"],
        volume_ratio=row["volume_ratio"],
        amount=row["amount"],
        tags=_display_tags(row),
        metrics=_json_float_dict(row["metrics_json"]),
        reason=row["reason"],
        error=row["error"],
        data_date=row["data_date"],
        quote_timestamp=row["quote_timestamp"],
        quote_source=row["quote_source"],
        kline_source=row["kline_source"],
        adjustment_mode=row["adjustment_mode"],
        quote_fallback_used=bool(row["quote_fallback_used"]),
        kline_fallback_used=bool(row["kline_fallback_used"]),
        metadata_degraded=bool(row["metadata_degraded"]),
        degradation_reasons=_structured_degradation_reasons(row),
        updated_at=row["updated_at"],
    )


def page_count(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total else 0


def append_exact_filter(
    clauses: list[str],
    params: list[object],
    column: str,
    value: object | None,
) -> None:
    if value is not None:
        clauses.append(f"{column} = ?")
        params.append(value)


def escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def result_order_sql(sort: MarketScanSort, order: MarketScanSortOrder) -> str:
    direction = "ASC" if order == "asc" else "DESC"
    primary = f"{sort} IS NULL ASC, {sort} {direction}"
    if sort == "rank":
        return f"{primary}, symbol ASC"
    return f"{primary}, score DESC, trend_score DESC, change_pct DESC, amount DESC, symbol ASC"


def percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(min(100.0, max(0.0, numerator / denominator * 100)), 2)


def _display_tags(row: sqlite3.Row) -> list[str]:
    tags = [tag for tag in _json_string_list(row["tags_json"]) if tag not in DEGRADATION_DISPLAY_TAGS]
    for enabled, label in (
        (bool(row["quote_fallback_used"]), "兜底行情"),
        (bool(row["kline_fallback_used"]), "兜底K线"),
        (bool(row["metadata_degraded"]), "上市日期未知"),
    ):
        if enabled:
            tags.append(label)
    return list(dict.fromkeys(tags))


def _structured_degradation_reasons(row: sqlite3.Row) -> list[str]:
    return [
        reason
        for enabled, reason in (
            (bool(row["quote_fallback_used"]), "quote_fallback"),
            (bool(row["kline_fallback_used"]), "kline_fallback"),
            (bool(row["metadata_degraded"]), "metadata_incomplete"),
        )
        if enabled
    ]


def _json_string_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _json_float_dict(value: object) -> dict[str, float]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in parsed.items():
        if isinstance(key, str) and isinstance(item, (int, float)) and math.isfinite(float(item)):
            result[key] = float(item)
    return result


__all__ = [
    "DEGRADATION_DISPLAY_TAGS",
    "append_exact_filter",
    "escaped_like",
    "page_count",
    "result_from_row",
    "result_order_sql",
    "run_from_row",
]
