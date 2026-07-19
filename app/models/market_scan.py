"""Contracts for full-market A-share scan runs and ranked results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MarketScanRunStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "success",
    "degraded",
    "failed",
    "cancelled",
    "interrupted",
]
MarketScanResultStatus = Literal["pending", "success", "missing", "skipped"]
MarketScanTrigger = Literal["manual", "scheduled", "retry"]
MarketScanSort = Literal[
    "rank",
    "score",
    "trend_score",
    "change_pct",
    "amount",
    "turnover_rate",
    "data_quality_score",
    "symbol",
]
MarketScanSortOrder = Literal["asc", "desc"]


@dataclass(frozen=True)
class MarketScanSeed:
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    is_st: bool = False
    is_new: bool = False
    metadata_source: str | None = None


@dataclass(frozen=True)
class MarketScanResultWrite:
    symbol: str
    status: MarketScanResultStatus
    score: int | None = None
    trend_score: int | None = None
    leader_score: int | None = None
    data_quality_score: int | None = None
    price: float | None = None
    change_pct: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    amount: float | None = None
    tags: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    reason: str | None = None
    error: str | None = None
    data_date: str | None = None
    quote_timestamp: str | None = None
    quote_source: str | None = None
    kline_source: str | None = None
    adjustment_mode: str | None = None
    quote_fallback_used: bool = False
    kline_fallback_used: bool = False
    metadata_degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketScanRetryPlan:
    source_run_id: int
    result_count: int
    preserved_success_count: int
    pending_count: int
    needs_market_data: bool


class MarketScanStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime | None = None


class MarketScanRun(BaseModel):
    id: int
    task_run_id: int | None = None
    retry_of_run_id: int | None = Field(default=None, ge=1)
    status: MarketScanRunStatus
    trigger: MarketScanTrigger
    rule_version: str
    as_of: str
    data_date: str
    scope: str
    stock_pool_source: str | None = None
    total_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    progress_pct: float = Field(ge=0, le=100)
    coverage_pct: float = Field(ge=0, le=100)
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    message: str | None = None
    last_error: str | None = None
    cancel_requested_at: str | None = None


class MarketScanStartResponse(BaseModel):
    accepted: bool
    deduplicated: bool = False
    run: MarketScanRun


class MarketScanResultItem(BaseModel):
    run_id: int
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    is_st: bool = False
    is_new: bool = False
    metadata_source: str | None = None
    status: MarketScanResultStatus
    rank: int | None = Field(default=None, ge=1)
    score: int | None = Field(default=None, ge=0, le=100)
    trend_score: int | None = Field(default=None, ge=0, le=100)
    leader_score: int | None = Field(default=None, ge=0, le=100)
    data_quality_score: int | None = Field(default=None, ge=0, le=100)
    price: float | None = None
    change_pct: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    amount: float | None = None
    tags: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    reason: str | None = None
    error: str | None = None
    data_date: str | None = None
    quote_timestamp: str | None = None
    quote_source: str | None = None
    kline_source: str | None = None
    adjustment_mode: str | None = None
    quote_fallback_used: bool = False
    kline_fallback_used: bool = False
    metadata_degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    updated_at: str


class MarketScanResultPage(BaseModel):
    run: MarketScanRun
    items: list[MarketScanResultItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    page_count: int = Field(ge=0)


class MarketScanRunPage(BaseModel):
    items: list[MarketScanRun]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    page_count: int = Field(ge=0)


__all__ = [
    "MarketScanResultItem",
    "MarketScanResultPage",
    "MarketScanResultStatus",
    "MarketScanResultWrite",
    "MarketScanRetryPlan",
    "MarketScanRun",
    "MarketScanRunPage",
    "MarketScanRunStatus",
    "MarketScanSort",
    "MarketScanSortOrder",
    "MarketScanStartRequest",
    "MarketScanStartResponse",
    "MarketScanSeed",
    "MarketScanTrigger",
]
