from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from typing import cast

from app.models.market_scan import (
    MARKET_SCAN_DEGRADATION_REASONS,
    MARKET_SCAN_METADATA_DEGRADATION_REASONS,
    MARKET_SCAN_RANK_TIE_BREAK,
    MarketScanResultItem,
    MarketScanRun,
    MarketScanMarketProgress,
    MarketScanPublicationDiagnostics,
    MarketScanStage,
    MarketScanStageMetric,
    MarketScanSort,
    MarketScanSortOrder,
)
from app.utils.time import non_negative_seconds_since_text


DEGRADATION_DISPLAY_TAGS = frozenset({"兜底行情", "兜底K线", "行业未知", "上市日期未知"})
METADATA_REASON_DISPLAY_TAGS = {
    "industry_missing": "行业未知",
    "list_date_missing": "上市日期未知",
    "metadata_incomplete": "上市日期未知",
}
MARKET_SCAN_RESULT_PAYLOAD_SCHEMA = "market-scan-result-payload-v2"


def run_from_row(row: sqlite3.Row) -> MarketScanRun:
    total = int(row["total_count"] or 0)
    processed = int(row["processed_count"] or 0)
    success = int(row["success_count"] or 0)
    skipped = int(row["skipped_count"] or 0)
    status = str(row["status"])
    elapsed_seconds = _run_elapsed_seconds(row, status)
    throughput = _run_throughput(processed, elapsed_seconds)
    return MarketScanRun(
        id=row["id"],
        task_run_id=row["task_run_id"],
        retry_of_run_id=row["retry_of_run_id"],
        status=status,
        trigger=row["trigger"],
        mode=_text_or(row["mode"], "official"),
        rule_version=row["rule_version"],
        as_of=row["as_of"],
        data_date=row["data_date"],
        quote_date=_text_or(row["quote_date"], str(row["data_date"])),
        scope=row["scope"],
        stock_pool_source=row["stock_pool_source"],
        total_count=total,
        excluded_count=int(row["excluded_count"] or 0),
        processed_count=processed,
        success_count=success,
        missing_count=int(row["missing_count"] or 0),
        skipped_count=skipped,
        retry_count=int(row["retry_count"] or 0),
        progress_pct=_run_progress(total, processed, status),
        coverage_pct=percentage(success, max(0, total - skipped)),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        quote_capture_started_at=row["quote_capture_started_at"],
        quote_capture_finished_at=row["quote_capture_finished_at"],
        quote_capture_duration_ms=row["quote_capture_duration_ms"],
        quote_capture_count=int(row["quote_capture_count"] or 0),
        current_stage=row["current_stage"],
        stage_started_at=row["stage_started_at"],
        stage_metrics=_stage_metrics(row["stage_metrics_json"]),
        market_progress=_market_progress(row["market_progress_json"]),
        elapsed_seconds=elapsed_seconds,
        throughput_per_second=throughput,
        eta_seconds=_run_eta(total, processed, elapsed_seconds, throughput),
        message=row["message"],
        last_error=row["last_error"],
        publication_diagnostics=_publication_diagnostics(row["publication_diagnostics_json"]),
        cancel_requested_at=row["cancel_requested_at"],
    )


def _text_or(value: object, fallback: str) -> str:
    return str(value) if value else fallback


def _run_progress(total: int, processed: int, status: str) -> float:
    if total == 0 and status in {"success", "degraded"}:
        return 100.0
    return percentage(processed, total)


def _run_throughput(processed: int, elapsed_seconds: float | None) -> float | None:
    if processed <= 0 or elapsed_seconds is None or elapsed_seconds < 1:
        return None
    return processed / elapsed_seconds


def _run_eta(
    total: int,
    processed: int,
    elapsed_seconds: float | None,
    throughput: float | None,
) -> float | None:
    if throughput is None or elapsed_seconds is None:
        return None
    if processed < 20 or elapsed_seconds < 5 or total <= processed:
        return None
    return max(0.0, (total - processed) / throughput)


def _run_elapsed_seconds(row: sqlite3.Row, status: str) -> float | None:
    duration_ms = row["duration_ms"]
    if status not in {"queued", "running", "cancelling"} and duration_ms is not None:
        return max(0.0, float(duration_ms) / 1000)
    return non_negative_seconds_since_text(row["started_at"])


def _stage_metrics(value: object) -> dict[MarketScanStage, MarketScanStageMetric]:
    parsed = _json_value(value, {})
    if not isinstance(parsed, dict):
        return {}
    allowed = {"stock_pool", "bulk_quotes", "klines", "scoring", "persistence", "publication"}
    metrics: dict[MarketScanStage, MarketScanStageMetric] = {}
    for stage, metric in parsed.items():
        if str(stage) not in allowed:
            continue
        try:
            metrics[cast(MarketScanStage, str(stage))] = MarketScanStageMetric.model_validate(metric)
        except (TypeError, ValueError):
            continue
    return metrics


def _market_progress(value: object) -> list[MarketScanMarketProgress]:
    parsed = _json_value(value, [])
    if not isinstance(parsed, list):
        return []
    progress: list[MarketScanMarketProgress] = []
    for item in parsed:
        try:
            progress.append(MarketScanMarketProgress.model_validate(item))
        except (TypeError, ValueError):
            continue
    return progress


def _publication_diagnostics(value: object) -> MarketScanPublicationDiagnostics | None:
    if value is None or not str(value).strip():
        return None
    parsed = _json_value(value, None)
    try:
        return MarketScanPublicationDiagnostics.model_validate(parsed)
    except (TypeError, ValueError):
        return None


def _json_value(value: object, fallback: object) -> object:
    if not isinstance(value, str):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def result_from_row(row: sqlite3.Row) -> MarketScanResultItem:
    metrics, score_details = decode_result_payload(row["metrics_json"])
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
        raw_score=row["raw_score"],
        trend_score=row["trend_score"],
        leader_score=row["leader_score"],
        data_quality_score=row["data_quality_score"],
        price=row["price"],
        change_pct=row["change_pct"],
        turnover_rate=row["turnover_rate"],
        volume_ratio=row["volume_ratio"],
        amount=row["amount"],
        tags=_display_tags(row),
        metrics=metrics,
        score_details=score_details,
        reason=row["reason"],
        error=row["error"],
        data_date=row["data_date"],
        quote_timestamp=row["quote_timestamp"],
        quote_observed_at=row["quote_observed_at"],
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


def result_order_sql(
    sort: MarketScanSort | Sequence[MarketScanSort],
    order: MarketScanSortOrder | Sequence[MarketScanSortOrder],
) -> str:
    sorts = (sort,) if isinstance(sort, str) else tuple(sort)
    orders = (order,) if isinstance(order, str) else tuple(order)
    if not sorts or len(sorts) != len(orders) or len(sorts) > 3:
        raise ValueError("排序字段和方向必须一一对应，且最多三级")
    if len(set(sorts)) != len(sorts):
        raise ValueError("排序字段不能重复")
    parts: list[str] = []
    for field, sort_order in zip(sorts, orders, strict=True):
        direction = "ASC" if sort_order == "asc" else "DESC"
        expression = _MARKET_SCAN_SORT_EXPRESSIONS[field]
        parts.extend((f"{expression} IS NULL ASC", f"{expression} {direction}"))
    if len(sorts) == 1 and sorts[0] == "rank":
        parts.append("symbol ASC")
    else:
        parts.append(rank_order_sql())
    return ", ".join(parts)


def rank_order_sql() -> str:
    return ", ".join(f"{column} {direction.upper()}" for column, direction in MARKET_SCAN_RANK_TIE_BREAK)


_SCORE_DIMENSION_JSON_PREFIX = "$.score_details.components.score_dimensions.scores"
_MARKET_SCAN_SORT_EXPRESSIONS: dict[MarketScanSort, str] = {
    "rank": "rank",
    "score": "score",
    "raw_score": "raw_score",
    "trend_score": "trend_score",
    "change_pct": "change_pct",
    "amount": "amount",
    "turnover_rate": "turnover_rate",
    "data_quality_score": "data_quality_score",
    "symbol": "symbol",
    "alpha_5d": f"json_extract(metrics_json, '{_SCORE_DIMENSION_JSON_PREFIX}.alpha_5d')",
    "confidence": f"json_extract(metrics_json, '{_SCORE_DIMENSION_JSON_PREFIX}.confidence')",
    "risk": f"json_extract(metrics_json, '{_SCORE_DIMENSION_JSON_PREFIX}.risk')",
    "tradability": f"json_extract(metrics_json, '{_SCORE_DIMENSION_JSON_PREFIX}.tradability')",
}


def percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(min(100.0, max(0.0, numerator / denominator * 100)), 2)


def _display_tags(row: sqlite3.Row) -> list[str]:
    tags = [tag for tag in _json_string_list(row["tags_json"]) if tag not in DEGRADATION_DISPLAY_TAGS]
    for enabled, label in (
        (bool(row["quote_fallback_used"]), "兜底行情"),
        (bool(row["kline_fallback_used"]), "兜底K线"),
    ):
        if enabled:
            tags.append(label)
    reasons = _structured_degradation_reasons(row)
    tags.extend(
        METADATA_REASON_DISPLAY_TAGS[reason]
        for reason in reasons
        if reason in METADATA_REASON_DISPLAY_TAGS
    )
    return list(dict.fromkeys(tags))


def _structured_degradation_reasons(row: sqlite3.Row) -> list[str]:
    persisted = [
        reason
        for reason in _json_string_list(row["degradation_reasons_json"])
        if reason in MARKET_SCAN_DEGRADATION_REASONS
    ]
    reasons: list[str] = []
    if bool(row["quote_fallback_used"]):
        reasons.append("quote_fallback")
    if bool(row["kline_fallback_used"]):
        reasons.append("kline_fallback")
    if bool(row["metadata_degraded"]):
        metadata_reasons = [
            reason for reason in persisted if reason in MARKET_SCAN_METADATA_DEGRADATION_REASONS
        ]
        reasons.extend(metadata_reasons or ["metadata_incomplete"])
    return list(dict.fromkeys(reasons))


def _json_string_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def encode_result_payload(
    metrics: dict[str, float],
    score_details: dict[str, object],
) -> str:
    return json.dumps(
        {
            "_schema": MARKET_SCAN_RESULT_PAYLOAD_SCHEMA,
            "metrics": metrics,
            "score_details": score_details,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_result_payload(value: object) -> tuple[dict[str, float], dict[str, object]]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}, {}
    if not isinstance(parsed, dict):
        return {}, {}
    if parsed.get("_schema") == MARKET_SCAN_RESULT_PAYLOAD_SCHEMA:
        metrics = _finite_float_dict(parsed.get("metrics"))
        score_details = parsed.get("score_details")
        return metrics, score_details if isinstance(score_details, dict) else {}
    return _finite_float_dict(parsed), {}


def _finite_float_dict(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, (int, float)) and math.isfinite(float(item)):
            result[key] = float(item)
    return result


__all__ = [
    "DEGRADATION_DISPLAY_TAGS",
    "MARKET_SCAN_RESULT_PAYLOAD_SCHEMA",
    "append_exact_filter",
    "decode_result_payload",
    "encode_result_payload",
    "escaped_like",
    "page_count",
    "result_from_row",
    "result_order_sql",
    "rank_order_sql",
    "run_from_row",
]
