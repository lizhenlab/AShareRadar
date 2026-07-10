from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.models.schemas import CacheFreshness, StorageDiagnostics, SystemDiagnostics
from app.services.provider_failure_status import (
    capability_recently_failed as provider_capability_recently_failed,
    provider_recently_failed,
)
from app.services.trading_calendar import calendar_source
from app.utils.market_data import finite_float
from app.utils.time import now_text


@dataclass(frozen=True)
class DiagnosticDecision:
    warning: str | None = None
    suggestion: str | None = None


@dataclass(frozen=True)
class CacheTimestampDiagnosticRule:
    timestamp_attr: str
    age_attr: str
    invalid_warning: str
    invalid_suggestion: str
    missing_warning: str | None = None
    missing_suggestion: str | None = None
    stale_threshold_seconds: int | None = None
    stale_warning: Callable[[int], str] | None = None
    stale_suggestion: str | None = None


def _quote_stale_warning(age_seconds: int) -> str:
    return f"最新报价缓存已超过 {age_seconds // 60} 分钟未更新。"


CACHE_TIMESTAMP_DIAGNOSTIC_RULES = (
    CacheTimestampDiagnosticRule(
        timestamp_attr="latest_quote_at",
        age_attr="latest_quote_age_seconds",
        missing_warning="尚未形成报价缓存。",
        missing_suggestion="打开任意个股或手动执行刷新报价。",
        invalid_warning="最新报价缓存时间异常。",
        invalid_suggestion="检查系统时间、数据源时间字段或清理异常缓存。",
        stale_threshold_seconds=15 * 60,
        stale_warning=_quote_stale_warning,
        stale_suggestion="执行刷新报价任务，或检查实时行情源是否可用。",
    ),
    CacheTimestampDiagnosticRule(
        timestamp_attr="latest_kline_at",
        age_attr="latest_kline_age_seconds",
        invalid_warning="最新日K缓存时间异常。",
        invalid_suggestion="检查系统时间、日K数据源时间字段或清理异常缓存。",
        stale_threshold_seconds=24 * 60 * 60,
        stale_suggestion="日K线缓存超过1天未刷新，建议手动执行关键个股K线刷新。",
    ),
    CacheTimestampDiagnosticRule(
        timestamp_attr="latest_minute_kline_at",
        age_attr="latest_minute_kline_age_seconds",
        invalid_warning="最新分钟K线缓存时间异常。",
        invalid_suggestion="检查系统时间、分钟K线数据源时间字段或清理异常缓存。",
    ),
)


def build_system_diagnostics(datahub, scheduler) -> SystemDiagnostics:
    cache_stats = datahub.cache.stats()
    providers = datahub.cache.provider_statuses()
    capability_statuses = datahub.cache.provider_capability_statuses()
    table_counts = _normalized_table_counts(datahub.cache.table_counts())
    scheduler_status = scheduler.status()
    checked_at = now_text()
    freshness = cache_freshness(cache_stats, checked_at)
    storage = storage_diagnostics(Path(cache_stats.path), table_counts)

    warnings: list[str] = []
    suggestions: list[str] = []
    _extend_cache_diagnostics(warnings, suggestions, cache_stats, freshness)
    _extend_provider_diagnostics(warnings, suggestions, providers, capability_statuses)
    _extend_capability_diagnostics(warnings, suggestions, datahub.capabilities())
    _extend_environment_diagnostics(warnings, suggestions, table_counts, storage, scheduler_status)

    return SystemDiagnostics(
        checked_at=checked_at,
        cache=cache_stats,
        freshness=freshness,
        storage=storage,
        scheduler=scheduler_status,
        providers=providers,
        table_counts=table_counts,
        warnings=_unique_texts(warnings),
        suggestions=_unique_texts(suggestions),
    )


def _extend_cache_diagnostics(warnings: list[str], suggestions: list[str], cache, freshness: CacheFreshness) -> None:
    for decision in _cache_diagnostic_decisions(cache, freshness):
        if decision.warning:
            warnings.append(decision.warning)
        if decision.suggestion:
            suggestions.append(decision.suggestion)


def _cache_diagnostic_decisions(cache, freshness: CacheFreshness) -> list[DiagnosticDecision]:
    return [
        decision
        for decision in (
            _cache_timestamp_decision(cache, freshness, rule)
            for rule in CACHE_TIMESTAMP_DIAGNOSTIC_RULES
        )
        if decision.warning or decision.suggestion
    ]


def _cache_timestamp_decision(cache, freshness: CacheFreshness, rule: CacheTimestampDiagnosticRule) -> DiagnosticDecision:
    timestamp = getattr(cache, rule.timestamp_attr, None)
    age = getattr(freshness, rule.age_attr, None)
    if not timestamp:
        return DiagnosticDecision(rule.missing_warning, rule.missing_suggestion)
    if age is None:
        return DiagnosticDecision(rule.invalid_warning, rule.invalid_suggestion)
    if rule.stale_threshold_seconds is not None and age > rule.stale_threshold_seconds:
        warning = rule.stale_warning(age) if rule.stale_warning else None
        return DiagnosticDecision(warning, rule.stale_suggestion)
    return DiagnosticDecision()


def _extend_provider_diagnostics(warnings: list[str], suggestions: list[str], providers, capability_statuses) -> None:
    decision = _provider_diagnostic_decision(providers, capability_statuses)
    if decision.warning:
        warnings.append(decision.warning)
    if decision.suggestion:
        suggestions.append(decision.suggestion)


def _provider_diagnostic_decision(providers, capability_statuses) -> DiagnosticDecision:
    unhealthy_capabilities = _unhealthy_capability_labels(capability_statuses)
    if unhealthy_capabilities:
        return DiagnosticDecision(
            warning="存在数据能力最近失败：" + _join_limited(unhealthy_capabilities, 6),
            suggestion="按失败能力检查网络、Token、本地客户端或源站连通性。",
        )
    unhealthy_providers = _unhealthy_provider_names(providers)
    if unhealthy_providers:
        return DiagnosticDecision(
            warning="存在数据源最近失败：" + _join_limited(unhealthy_providers, 5),
            suggestion="检查网络、Token 或数据源依赖安装状态。",
        )
    return DiagnosticDecision()


def _unhealthy_capability_labels(capability_statuses) -> list[str]:
    return _unique_texts(_capability_failure_label(item) for item in capability_statuses or [] if provider_capability_recently_failed(item))


def _capability_failure_label(item) -> str:
    name = _clean_text(getattr(item, "name", None)) or "未知数据源"
    return f"{name} {capability_label(getattr(item, 'kind', None))}"


def _unhealthy_provider_names(providers) -> list[str]:
    return _unique_texts((_clean_text(getattr(item, "name", None)) or "未知数据源") for item in providers or [] if provider_recently_failed(item))


def _join_limited(items: list[str], limit: int) -> str:
    return "、".join(_unique_texts(items)[:limit])


def _extend_capability_diagnostics(warnings: list[str], suggestions: list[str], capabilities) -> None:
    capability_list = list(capabilities)
    if _enabled_realtime_quote_source_count(capability_list) < 2:
        warnings.append("可用实时报价源少于2个，多源一致性校验能力不足。")
        suggestions.append("建议启用 Futu OpenAPI、Tushare 或修复 AKShare，以提升行情交叉验证能力。")
    if _demo_capability_enabled(capability_list):
        warnings.append("演示行情源已启用，当前环境不适合输出真实个股建议。")
        suggestions.append("关闭 ASHARE_RADAR_DEMO_PROVIDER_ENABLED，或只用于离线演示。")


def _enabled_realtime_quote_source_count(capabilities) -> int:
    return len(_real_realtime_quote_source_names(capabilities))


def _real_realtime_quote_source_names(capabilities) -> list[str]:
    return _unique_texts(
        (_clean_text(getattr(item, "name", None)) or "未知数据源")
        for item in capabilities or []
        if _is_real_realtime_quote_source(item)
    )


def _is_real_realtime_quote_source(item) -> bool:
    return bool(
        getattr(item, "enabled", False)
        and getattr(item, "realtime_quote", False)
        and _clean_text(getattr(item, "reliability_level", None)) != "演示"
    )


def _demo_capability_enabled(capabilities) -> bool:
    return any(getattr(item, "enabled", False) and _clean_text(getattr(item, "reliability_level", None)) == "演示" for item in capabilities or [])


def _extend_environment_diagnostics(warnings: list[str], suggestions: list[str], table_counts, storage: StorageDiagnostics, scheduler_status) -> None:
    if calendar_source() == "工作日兜底":
        warnings.append("交易日历未缓存，当前按普通工作日判断行情新鲜度。")
        suggestions.append("可设置 ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=1 或调用交易日历刷新逻辑，以降低节假日误判。")
    if _table_count(table_counts, "alert_rule") and not getattr(scheduler_status, "running", False):
        suggestions.append("存在本地预警但调度器未运行，建议启动调度器或手动评估。")
    if storage.db_size_mb > 512:
        suggestions.append("本地缓存文件较大，建议执行运行期清理或缩短历史保留上限。")


def cache_freshness(cache, checked_at: str) -> CacheFreshness:
    return CacheFreshness(
        latest_quote_age_seconds=age_seconds(cache.latest_quote_at, checked_at),
        latest_kline_age_seconds=age_seconds(cache.latest_kline_at, checked_at),
        latest_minute_kline_age_seconds=age_seconds(getattr(cache, "latest_minute_kline_at", None), checked_at),
        latest_stock_age_seconds=age_seconds(cache.latest_stock_at, checked_at),
        latest_plate_age_seconds=age_seconds(cache.latest_plate_at, checked_at),
    )


def age_seconds(value: str | None, checked_at: str) -> int | None:
    if not value:
        return None
    try:
        age = int((datetime.fromisoformat(checked_at) - datetime.fromisoformat(value)).total_seconds())
    except (TypeError, ValueError):
        return None
    if age < 0:
        return None
    return age


def storage_diagnostics(path: Path, table_counts: dict[str, int]) -> StorageDiagnostics:
    runtime_tables = {"quote_history", "cache_event", "task_run", "monitor_event", "advice_history", "alert_event"}
    user_tables = {"watchlist", "alert_rule", "stock_note"}
    table_counts = _normalized_table_counts(table_counts)
    size_bytes = path.stat().st_size if path.exists() else 0
    runtime_rows = sum(_table_count(table_counts, table) for table in runtime_tables)
    user_rows = sum(_table_count(table_counts, table) for table in user_tables)
    return StorageDiagnostics(
        db_path=str(path),
        db_size_bytes=size_bytes,
        db_size_mb=round(size_bytes / 1024 / 1024, 2),
        runtime_rows=runtime_rows,
        user_rows=user_rows,
    )


def _table_count(table_counts, table: str) -> int:
    raw_value = table_counts.get(table, 0) if hasattr(table_counts, "get") else 0
    return _positive_count(raw_value)


def _positive_count(raw_value: object) -> int:
    value = finite_float(raw_value)
    if value is None or value <= 0:
        return 0
    return int(value)


def _normalized_table_counts(table_counts) -> dict[str, int]:
    if not hasattr(table_counts, "items"):
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_value in table_counts.items():
        key = _clean_text(raw_key)
        if key is None:
            continue
        normalized[key] = max(normalized.get(key, 0), _positive_count(raw_value))
    return normalized


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            float(value)
        except (TypeError, ValueError):
            text = " ".join(str(value).split())
        else:
            return None
    invalid_text = {"nan", "none", "null", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
    return text if text and text.lower() not in invalid_text else None


def _unique_texts(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _clean_text(item)
        key = text.casefold() if text is not None else None
        if text is None or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def capability_label(kind: str) -> str:
    kind = _clean_text(kind) or ""
    labels = {
        "quote": "报价",
        "kline": "日K",
        "minute": "分钟线",
        "stock": "股票池",
        "plate": "板块",
        "concept": "概念",
        "order_book": "盘口",
    }
    return labels.get(kind, kind or "未知能力")


__all__ = [
    "age_seconds",
    "build_system_diagnostics",
    "cache_freshness",
    "capability_label",
    "storage_diagnostics",
]
