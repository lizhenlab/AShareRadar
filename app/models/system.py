"""Runtime status, diagnostics, provider health, and scheduler models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.market import ProviderCapability


class ProviderStatus(BaseModel):
    name: str
    enabled: bool
    priority: int
    healthy: bool
    last_success: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    updated_at: str | None = None


class ProviderCapabilityStatus(BaseModel):
    name: str
    kind: str
    enabled: bool
    priority: int
    healthy: bool
    last_success: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    updated_at: str | None = None


class ProviderDecision(BaseModel):
    name: str
    role: str
    state: str
    priority: int
    capabilities: list[str] = Field(default_factory=list)
    success_rate: float | None = None
    last_success: str | None = None
    last_error: str | None = None
    action: str


class DataSourcePlan(BaseModel):
    primary_quote_source: str | None = None
    primary_kline_source: str | None = None
    primary_minute_source: str | None = None
    health_level: str
    summary: str
    decisions: list[ProviderDecision] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CacheStats(BaseModel):
    path: str
    quote_count: int
    quote_history_count: int
    kline_count: int
    daily_kline_count: int = 0
    minute_kline_count: int = 0
    stock_count: int
    plate_count: int
    provider_count: int
    latest_quote_at: str | None = Field(default=None, description="兼容字段：最近报价抓取时间。")
    latest_kline_at: str | None = Field(default=None, description="兼容字段：最近日K抓取时间。")
    latest_daily_kline_at: str | None = Field(default=None, description="兼容字段：最近日K抓取时间。")
    latest_minute_kline_at: str | None = Field(default=None, description="兼容字段：最近分钟K抓取时间。")
    latest_quote_fetched_at: str | None = Field(default=None, description="最近报价抓取时间。")
    latest_daily_kline_fetched_at: str | None = Field(default=None, description="最近日K抓取时间。")
    latest_minute_kline_fetched_at: str | None = Field(default=None, description="最近分钟K抓取时间。")
    latest_quote_timestamp: str | None = Field(default=None, description="最近报价市场事件时间。")
    latest_daily_kline_date: str | None = Field(default=None, description="最近日K市场交易日期。")
    latest_minute_kline_timestamp: str | None = Field(default=None, description="最近分钟K市场事件时间。")
    latest_stock_at: str | None = None
    latest_plate_at: str | None = None


class DataStatus(BaseModel):
    providers: list[ProviderStatus]
    cache: CacheStats
    capabilities: list[ProviderCapability] = Field(default_factory=list)
    capability_statuses: list[ProviderCapabilityStatus] = Field(default_factory=list)
    source_plan: DataSourcePlan | None = None


class FutuStatusResponse(BaseModel):
    ok: bool
    message: str
    latency_ms: float | None = None


class TradeCalendarRefreshResponse(BaseModel):
    ok: bool
    trade_date_count: int
    source: str
    error: str | None = None


class TaskRunOnceResponse(BaseModel):
    ok: bool
    messages: list[str] = Field(default_factory=list)


class MutationResult(BaseModel):
    ok: bool
    removed: bool


class TaskRun(BaseModel):
    id: int
    task_name: str
    status: str
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    message: str | None = None


class MonitorEvent(BaseModel):
    id: int
    level: str
    category: str
    symbol: str | None = None
    message: str
    created_at: str
    last_seen_at: str | None = None
    repeat_count: int = 1


class ScheduledTaskState(BaseModel):
    name: str
    display_name: str
    interval_seconds: int
    running: bool
    last_started_at: str | None = None
    last_finished_at: str | None = None
    next_run_at: str | None = None
    last_status: str | None = None
    last_message: str | None = None


class SchedulerStatus(BaseModel):
    enabled: bool
    running: bool
    standby: bool = False
    message: str | None = None
    started_at: str | None = None
    task_count: int
    tasks: list[ScheduledTaskState]


class FreshnessObservation(BaseModel):
    status: str
    observed_at: str | None = None
    age_seconds: int | None = None
    detail: str | None = None


class CacheFreshness(BaseModel):
    latest_quote_age_seconds: int | None = Field(default=None, description="兼容字段：最近报价抓取年龄。")
    latest_kline_age_seconds: int | None = Field(default=None, description="兼容字段：最近日K抓取年龄。")
    latest_minute_kline_age_seconds: int | None = Field(default=None, description="兼容字段：最近分钟K抓取年龄。")
    latest_stock_age_seconds: int | None = Field(default=None, description="兼容字段：最近股票池更新时间年龄。")
    latest_plate_age_seconds: int | None = Field(default=None, description="兼容字段：最近行业背景更新时间年龄。")
    latest_quote_fetch_age_seconds: int | None = None
    latest_daily_kline_fetch_age_seconds: int | None = None
    latest_minute_kline_fetch_age_seconds: int | None = None
    latest_stock_fetch_age_seconds: int | None = None
    latest_plate_fetch_age_seconds: int | None = None
    fetch_activity: dict[str, FreshnessObservation] = Field(default_factory=dict)
    market_freshness: dict[str, FreshnessObservation] = Field(default_factory=dict)
    checked_domains: list[str] = Field(default_factory=list)


class StorageDiagnostics(BaseModel):
    db_path: str
    db_size_bytes: int
    db_size_mb: float
    sqlite_size_bytes: int = 0
    backup_size_bytes: int = 0
    managed_backup_count: int = 0
    cache_rows: int
    runtime_rows: int
    user_rows: int
    quote_rows: int = 0
    kline_rows: int = 0
    market_scan_rows: int = 0
    other_cache_rows: int = 0
    other_runtime_rows: int = 0
    budget_bytes: int
    warning_at_pct: float
    usage_pct: float
    over_budget: bool


class SystemDiagnostics(BaseModel):
    checked_at: str
    cache: CacheStats
    freshness: CacheFreshness
    storage: StorageDiagnostics
    scheduler: SchedulerStatus
    providers: list[ProviderStatus]
    table_counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
