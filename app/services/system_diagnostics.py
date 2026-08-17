from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import DEFAULT_MAX_DATABASE_SIZE_MB
from app.models.local_data import USER_DATA_TABLE_ALLOWLIST
from app.models.system import CacheFreshness, FreshnessObservation as FreshnessObservationModel, StorageDiagnostics, SystemDiagnostics
from app.services.cache_freshness import CacheFreshnessAssessment, FreshnessObservation, assess_cache_freshness
from app.services.provider_failure_status import (
    capability_recently_failed as provider_capability_recently_failed,
    provider_recently_failed,
)
from app.services.research_artifact_catalog import research_artifact_storage
from app.services.runtime_backup import runtime_backup_storage
from app.services.trading_calendar import TradeCalendarSource, TradeCalendarStatus, calendar_status
from app.utils.audit_time import audit_datetime_to_text, normalize_audit_time_text, parse_audit_time
from app.utils.clock import market_now_naive
from app.utils.market_data import finite_float
from app.utils.market_time import market_local_naive
from app.utils.text import clean_optional_text as _clean_text
from app.utils.time import parse_text_time


@dataclass(frozen=True)
class DiagnosticDecision:
    warning: str | None = None
    suggestion: str | None = None


STORAGE_WARNING_AT_PCT = 80.0
RESEARCH_STORAGE_NOTICE_AT_PCT = 10.0
QUOTE_STORAGE_TABLES = frozenset({"quote_snapshot", "quote_history"})
KLINE_STORAGE_TABLES = frozenset({"kline_daily", "kline_minute"})
MARKET_SCAN_STORAGE_TABLES = frozenset({"market_scan_run", "market_scan_result"})
OTHER_CACHE_DATA_TABLES = frozenset(
    {"provider_status", "provider_capability_status", "stock_master", "plate_rank", "stock_concept"}
)
CACHE_DATA_TABLES = QUOTE_STORAGE_TABLES | KLINE_STORAGE_TABLES | OTHER_CACHE_DATA_TABLES
RUNTIME_STATE_TABLES = frozenset(
    {"cache_event", "task_run", "monitor_event", "reliability_bucket"}
) | MARKET_SCAN_STORAGE_TABLES
SQLITE_STORAGE_COMPONENT_SUFFIXES = {
    "main": "",
    "wal": "-wal",
    "shm": "-shm",
}


def build_system_diagnostics(datahub, scheduler, *, now: datetime | None = None) -> SystemDiagnostics:
    current = market_local_naive(now) if now is not None else market_now_naive()
    cache_stats = datahub.cache.stats()
    providers = datahub.cache.provider_statuses()
    capability_statuses = datahub.cache.provider_capability_statuses()
    table_counts = _normalized_table_counts(datahub.cache.table_counts())
    scheduler_status = scheduler.status()
    checked_at = audit_datetime_to_text(current)
    trade_calendar = calendar_status(current.date())
    assessment = (
        assess_cache_freshness(
            cache_stats,
            now=current,
            stock_pool_cache_seconds=getattr(getattr(datahub, "settings", None), "stock_pool_cache_seconds", 24 * 60 * 60),
            plate_rank_cache_seconds=getattr(getattr(datahub, "settings", None), "plate_rank_cache_seconds", 10 * 60),
        )
        if trade_calendar.covered
        else CacheFreshnessAssessment(domains=(), availability_issues=(), checked_domains=())
    )
    freshness = cache_freshness(cache_stats, checked_at, assessment=assessment)
    budget_mb = getattr(
        getattr(datahub, "settings", None),
        "max_database_size_mb",
        DEFAULT_MAX_DATABASE_SIZE_MB,
    )
    storage = storage_diagnostics(Path(cache_stats.path), table_counts, budget_mb=budget_mb)

    warnings: list[str] = []
    suggestions: list[str] = []
    _extend_cache_diagnostics(warnings, suggestions, assessment)
    _extend_provider_diagnostics(warnings, suggestions, providers, capability_statuses)
    _extend_capability_diagnostics(warnings, suggestions, datahub.capabilities())
    _extend_environment_diagnostics(warnings, suggestions, table_counts, storage, scheduler_status, trade_calendar)

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


def _extend_cache_diagnostics(
    warnings: list[str],
    suggestions: list[str],
    assessment: CacheFreshnessAssessment,
) -> None:
    for issue in assessment.issues:
        warnings.append(issue.message)
        suggestions.append(issue.suggestion)


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
    return _unique_texts((_clean_text(getattr(item, "name", None)) or "未知数据源") for item in capabilities or [] if _is_real_realtime_quote_source(item))


def _is_real_realtime_quote_source(item) -> bool:
    return bool(getattr(item, "enabled", False) and getattr(item, "realtime_quote", False) and _clean_text(getattr(item, "reliability_level", None)) != "演示")


def _demo_capability_enabled(capabilities) -> bool:
    return any(getattr(item, "enabled", False) and _clean_text(getattr(item, "reliability_level", None)) == "演示" for item in capabilities or [])


def _extend_environment_diagnostics(
    warnings: list[str],
    suggestions: list[str],
    table_counts,
    storage: StorageDiagnostics,
    scheduler_status,
    trade_calendar: TradeCalendarStatus,
) -> None:
    if trade_calendar.source is TradeCalendarSource.OUT_OF_COVERAGE:
        warnings.append("交易日历未覆盖当前日期，已跳过依赖交易日期的行情新鲜度判断并保守关闭交易任务。")
        suggestions.append("调用 POST /api/data/trading-calendar/refresh 刷新运行时日历；进入新年度前同时更新 bundled baseline。")
    elif trade_calendar.source is TradeCalendarSource.UNAVAILABLE:
        warnings.append("运行时交易日历与内置基线均不可用，已跳过行情交易日期判断并保守关闭交易任务。")
        suggestions.append("检查 app/resources/trading_calendar.json 完整性，并调用交易日历刷新 API 重建 data/ 运行时缓存。")
    elif trade_calendar.warning:
        warnings.append(trade_calendar.warning)
        suggestions.append("调用 POST /api/data/trading-calendar/refresh 重建运行时交易日历缓存。")
    if (
        _table_count(table_counts, "alert_rule")
        and not getattr(scheduler_status, "running", False)
        and not getattr(scheduler_status, "standby", False)
    ):
        suggestions.append("存在本地预警但调度器未运行，建议启动调度器或手动评估。")
    if storage.over_budget:
        warnings.append("本地数据库已超过容量预算。")
        suggestions.append("先备份用户数据，再执行运行期清理或缩短可再生缓存保留上限。")
    elif storage.usage_pct >= storage.warning_at_pct:
        warnings.append("本地数据库容量已接近预算上限。")
        suggestions.append("建议预览运行期清理结果，并检查行情缓存保留上限。")
    _extend_research_storage_diagnostics(warnings, suggestions, storage)


def _extend_research_storage_diagnostics(
    warnings: list[str],
    suggestions: list[str],
    storage: StorageDiagnostics,
) -> None:
    research_usage_pct = storage.research_artifact_size_bytes / storage.budget_bytes * 100
    if storage.total_managed_over_budget and not storage.over_budget:
        warnings.append("本地受管存储（数据库、备份与研究证据）已超过数据库容量预算参考值。")
        suggestions.append("查看研究 artifact 的预览式保留摘要；确认引用与完整性前不要删除研究证据。")
    elif storage.total_managed_usage_pct >= storage.warning_at_pct and storage.usage_pct < storage.warning_at_pct:
        warnings.append("本地受管存储（数据库、备份与研究证据）已接近数据库容量预算参考值。")
        suggestions.append("检查研究证据目录占用，并将大型探索产物迁移到单独监控的卷。")
    elif research_usage_pct >= RESEARCH_STORAGE_NOTICE_AT_PCT:
        warnings.append("研究证据归档占用已达到数据库容量预算参考值的 10%。")
        suggestions.append("定期检查研究 artifact 目录；仅按预览摘要人工核验，不执行自动删除。")
    if storage.research_artifacts.ignored_symlink_count:
        warnings.append("研究 artifact 目录中存在符号链接，安全扫描已忽略这些条目。")


def cache_freshness(
    cache,
    checked_at: str | datetime,
    *,
    assessment: CacheFreshnessAssessment | None = None,
) -> CacheFreshness:
    current = _checked_datetime(checked_at)
    checked_text = _checked_text(checked_at)
    if current is None:
        return CacheFreshness(
            latest_quote_age_seconds=age_seconds(cache.latest_quote_at, checked_text),
            latest_kline_age_seconds=age_seconds(cache.latest_kline_at, checked_text),
            latest_minute_kline_age_seconds=age_seconds(getattr(cache, "latest_minute_kline_at", None), checked_text),
            latest_stock_age_seconds=age_seconds(cache.latest_stock_at, checked_text),
            latest_plate_age_seconds=age_seconds(cache.latest_plate_at, checked_text),
        )

    assessment = assessment or assess_cache_freshness(cache, now=current)
    fetch_activity = assessment.fetch_activity
    quote_fetch_age = _observation_age(fetch_activity.get("quote"))
    daily_fetch_age = _observation_age(fetch_activity.get("daily_kline"))
    minute_fetch_age = _observation_age(fetch_activity.get("minute_kline"))
    stock_fetch_age = _observation_age(fetch_activity.get("stock"))
    plate_fetch_age = _observation_age(fetch_activity.get("plate"))
    return CacheFreshness(
        latest_quote_age_seconds=quote_fetch_age,
        latest_kline_age_seconds=daily_fetch_age,
        latest_minute_kline_age_seconds=minute_fetch_age,
        latest_stock_age_seconds=stock_fetch_age,
        latest_plate_age_seconds=plate_fetch_age,
        latest_quote_fetch_age_seconds=quote_fetch_age,
        latest_daily_kline_fetch_age_seconds=daily_fetch_age,
        latest_minute_kline_fetch_age_seconds=minute_fetch_age,
        latest_stock_fetch_age_seconds=stock_fetch_age,
        latest_plate_fetch_age_seconds=plate_fetch_age,
        fetch_activity={key: _observation_model(value) for key, value in fetch_activity.items()},
        market_freshness={key: _observation_model(value) for key, value in assessment.market_freshness.items()},
        checked_domains=list(assessment.checked_domains),
    )


def _observation_age(observation: FreshnessObservation | None) -> int | None:
    return observation.age_seconds if observation is not None else None


def _observation_model(observation: FreshnessObservation) -> FreshnessObservationModel:
    return FreshnessObservationModel(
        status=observation.status,
        observed_at=observation.observed_at,
        age_seconds=observation.age_seconds,
        detail=observation.detail,
    )


def _checked_datetime(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return market_local_naive(value)
    try:
        return parse_text_time(value)
    except (TypeError, ValueError):
        return None


def _checked_text(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return audit_datetime_to_text(value)
    try:
        return normalize_audit_time_text(value)
    except (TypeError, ValueError):
        return value


def age_seconds(value: str | None, checked_at: str) -> int | None:
    if not value:
        return None
    try:
        age = int((parse_audit_time(checked_at) - parse_audit_time(value)).total_seconds())
    except (TypeError, ValueError):
        return None
    if age < 0:
        return None
    return age


def storage_diagnostics(
    path: Path,
    table_counts: dict[str, int],
    *,
    budget_mb: object = DEFAULT_MAX_DATABASE_SIZE_MB,
) -> StorageDiagnostics:
    table_counts = _normalized_table_counts(table_counts)
    component_sizes = _sqlite_component_sizes(path)
    backup_storage = runtime_backup_storage(path) if str(path) != ":memory:" else None
    sqlite_size_bytes = sum(component_sizes.values())
    backup_size_bytes = backup_storage.size_bytes if backup_storage is not None else 0
    size_bytes = sqlite_size_bytes + backup_size_bytes
    budget_bytes = _storage_budget_bytes(budget_mb)
    research_artifacts = research_artifact_storage(path)
    total_managed_size_bytes = size_bytes + research_artifacts.size_bytes
    quote_rows = _table_group_count(table_counts, QUOTE_STORAGE_TABLES)
    kline_rows = _table_group_count(table_counts, KLINE_STORAGE_TABLES)
    market_scan_rows = _table_group_count(table_counts, MARKET_SCAN_STORAGE_TABLES)
    other_cache_rows = _table_group_count(table_counts, OTHER_CACHE_DATA_TABLES)
    other_runtime_rows = _table_group_count(table_counts, RUNTIME_STATE_TABLES - MARKET_SCAN_STORAGE_TABLES)
    user_rows = sum(_table_count(table_counts, table) for table in USER_DATA_TABLE_ALLOWLIST)
    usage_pct = round(size_bytes / budget_bytes * 100, 2)
    return StorageDiagnostics(
        db_path=str(path),
        db_size_bytes=size_bytes,
        db_size_mb=round(size_bytes / 1024 / 1024, 2),
        sqlite_size_bytes=sqlite_size_bytes,
        backup_size_bytes=backup_size_bytes,
        managed_backup_count=backup_storage.managed_bundle_count if backup_storage is not None else 0,
        cache_rows=quote_rows + kline_rows + other_cache_rows,
        runtime_rows=market_scan_rows + other_runtime_rows,
        user_rows=user_rows,
        quote_rows=quote_rows,
        kline_rows=kline_rows,
        market_scan_rows=market_scan_rows,
        other_cache_rows=other_cache_rows,
        other_runtime_rows=other_runtime_rows,
        budget_bytes=budget_bytes,
        warning_at_pct=STORAGE_WARNING_AT_PCT,
        usage_pct=usage_pct,
        over_budget=size_bytes > budget_bytes,
        research_artifacts=research_artifacts,
        research_artifact_size_bytes=research_artifacts.size_bytes,
        research_artifact_size_mb=round(research_artifacts.size_bytes / 1024 / 1024, 2),
        total_managed_size_bytes=total_managed_size_bytes,
        total_managed_size_mb=round(total_managed_size_bytes / 1024 / 1024, 2),
        total_managed_usage_pct=round(total_managed_size_bytes / budget_bytes * 100, 2),
        total_managed_over_budget=total_managed_size_bytes > budget_bytes,
    )


def _table_group_count(table_counts: dict[str, int], tables: frozenset[str]) -> int:
    return sum(_table_count(table_counts, table) for table in tables)


def _sqlite_component_sizes(path: Path) -> dict[str, int]:
    return {component: _file_size(Path(f"{path}{suffix}")) for component, suffix in SQLITE_STORAGE_COMPONENT_SUFFIXES.items()}


def _file_size(path: Path) -> int:
    try:
        return max(0, path.stat().st_size)
    except OSError:
        return 0


def _storage_budget_bytes(budget_mb: object) -> int:
    value = finite_float(budget_mb)
    if value is None or value < 16:
        value = DEFAULT_MAX_DATABASE_SIZE_MB
    return int(value * 1024 * 1024)


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


def _unique_texts(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _clean_text(item)
        if text is None:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def capability_label(kind: object) -> str:
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
