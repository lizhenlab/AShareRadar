from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_datahub, get_scheduler
from app.api.errors import run_api
from app.models.schemas import CacheFreshness, MonitorEvent, SchedulerStatus, StorageDiagnostics, SystemDiagnostics, TaskRun
from app.services.datahub import DataHub
from app.services.scheduler import LocalDataScheduler
from app.services.trading_calendar import calendar_source
from app.utils.time import now_text


router = APIRouter()


@router.get("/api/tasks/status", response_model=SchedulerStatus)
async def task_status(scheduler: LocalDataScheduler = Depends(get_scheduler)) -> SchedulerStatus:
    return scheduler.status()


@router.get("/api/tasks/runs", response_model=list[TaskRun])
async def task_runs(
    limit: int = Query(20, ge=1, le=100),
    datahub: DataHub = Depends(get_datahub),
) -> list[TaskRun]:
    return datahub.cache.recent_task_runs(limit=limit)


@router.get("/api/monitor/events", response_model=list[MonitorEvent])
async def monitor_events(
    limit: int = Query(30, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[MonitorEvent]:
    return datahub.cache.recent_monitor_events(limit=limit)


@router.post("/api/tasks/run-once")
async def run_task_once(
    task: str | None = Query(None, description="任务名称，不填则按顺序执行全部本地刷新任务"),
    scheduler: LocalDataScheduler = Depends(get_scheduler),
) -> dict[str, object]:
    async def run() -> dict[str, object]:
        messages = await scheduler.run_once(task)
        return {"ok": True, "messages": messages}

    return await run_api(run)


@router.get("/api/system/diagnostics", response_model=SystemDiagnostics)
async def system_diagnostics(
    datahub: DataHub = Depends(get_datahub),
    scheduler: LocalDataScheduler = Depends(get_scheduler),
) -> SystemDiagnostics:
    cache = datahub.cache.stats()
    providers = datahub.cache.provider_statuses()
    capability_statuses = datahub.cache.provider_capability_statuses()
    table_counts = datahub.cache.table_counts()
    checked_at = now_text()
    freshness = _cache_freshness(cache, checked_at)
    storage = _storage_diagnostics(Path(cache.path), table_counts)
    warnings: list[str] = []
    suggestions: list[str] = []
    if not cache.latest_quote_at:
        warnings.append("尚未形成报价缓存。")
        suggestions.append("打开任意个股或手动执行刷新报价。")
    elif freshness.latest_quote_age_seconds is not None and freshness.latest_quote_age_seconds > 15 * 60:
        warnings.append(f"最新报价缓存已超过 {freshness.latest_quote_age_seconds // 60} 分钟未更新。")
        suggestions.append("执行刷新报价任务，或检查实时行情源是否可用。")
    if freshness.latest_kline_age_seconds is not None and freshness.latest_kline_age_seconds > 24 * 60 * 60:
        suggestions.append("日K线缓存超过1天未刷新，建议手动执行关键个股K线刷新。")
    unhealthy_capabilities = [
        f"{item.name} {_capability_label(item.kind)}"
        for item in capability_statuses
        if item.enabled and not item.healthy and (item.last_error or item.failure_count)
    ]
    unhealthy = [item.name for item in providers if item.enabled and not item.healthy]
    if unhealthy_capabilities:
        warnings.append("存在数据能力最近失败：" + "、".join(unhealthy_capabilities[:6]))
        suggestions.append("按失败能力检查网络、Token、本地客户端或源站连通性。")
    elif unhealthy:
        warnings.append("存在数据源最近失败：" + "、".join(unhealthy[:5]))
        suggestions.append("检查网络、Token 或数据源依赖安装状态。")
    enabled_quote_sources = [
        item.name
        for item in datahub.capabilities()
        if item.enabled and item.realtime_quote and item.reliability_level != "演示"
    ]
    if len(enabled_quote_sources) < 2:
        warnings.append("可用实时报价源少于2个，多源一致性校验能力不足。")
        suggestions.append("建议启用 Futu OpenAPI、Tushare 或修复 AKShare，以提升行情交叉验证能力。")
    if any(item.enabled and item.reliability_level == "演示" for item in datahub.capabilities()):
        warnings.append("演示行情源已启用，当前环境不适合输出真实个股建议。")
        suggestions.append("关闭 DEMO_PROVIDER_ENABLED，或只用于离线演示。")
    if calendar_source() == "工作日兜底":
        warnings.append("交易日历未缓存，当前按普通工作日判断行情新鲜度。")
        suggestions.append("可设置 TRADE_CALENDAR_AUTO_FETCH=1 或调用交易日历刷新逻辑，以降低节假日误判。")
    if table_counts.get("alert_rule", 0) and not scheduler.status().running:
        suggestions.append("存在本地预警但调度器未运行，建议启动调度器或手动评估。")
    if storage.db_size_mb > 512:
        suggestions.append("本地缓存文件较大，建议执行运行期清理或缩短历史保留上限。")
    return SystemDiagnostics(
        checked_at=checked_at,
        cache=cache,
        freshness=freshness,
        storage=storage,
        scheduler=scheduler.status(),
        providers=providers,
        table_counts=table_counts,
        warnings=warnings,
        suggestions=suggestions,
    )


def _cache_freshness(cache, checked_at: str) -> CacheFreshness:
    return CacheFreshness(
        latest_quote_age_seconds=_age_seconds(cache.latest_quote_at, checked_at),
        latest_kline_age_seconds=_age_seconds(cache.latest_kline_at, checked_at),
        latest_stock_age_seconds=_age_seconds(cache.latest_stock_at, checked_at),
        latest_plate_age_seconds=_age_seconds(cache.latest_plate_at, checked_at),
    )


def _age_seconds(value: str | None, checked_at: str) -> int | None:
    if not value:
        return None
    try:
        return max(0, int((datetime.fromisoformat(checked_at) - datetime.fromisoformat(value)).total_seconds()))
    except ValueError:
        return None


def _storage_diagnostics(path: Path, table_counts: dict[str, int]) -> StorageDiagnostics:
    runtime_tables = {"quote_history", "task_run", "monitor_event", "advice_history", "alert_event"}
    user_tables = {"watchlist", "alert_rule", "stock_note"}
    size_bytes = path.stat().st_size if path.exists() else 0
    runtime_rows = sum(table_counts.get(table, 0) for table in runtime_tables)
    user_rows = sum(table_counts.get(table, 0) for table in user_tables)
    return StorageDiagnostics(
        db_path=str(path),
        db_size_bytes=size_bytes,
        db_size_mb=round(size_bytes / 1024 / 1024, 2),
        runtime_rows=runtime_rows,
        user_rows=user_rows,
    )


def _capability_label(kind: str) -> str:
    labels = {
        "quote": "报价",
        "kline": "日K",
        "minute": "分钟",
        "stock": "股票池",
        "plate": "板块",
        "concept": "概念",
        "order_book": "盘口",
    }
    return labels.get(kind, kind)
