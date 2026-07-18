"""User-managed watchlist, alert, note, chart mark, and advice-history models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, FiniteFloat

from app.models.market import KlineAdjustmentMode


ResearchStatus = Literal["to_research", "watching", "holding_research", "excluded"]
WatchlistPriority = Literal["high", "medium", "low"]
AdviceComparisonStatus = Literal["comparable", "no_previous", "legacy", "version_changed"]
AdviceChangeDirection = Literal["up", "down", "changed", "not_comparable"]
AdviceChangeCategory = Literal["action", "advice", "trend", "risk", "price_level", "data_quality"]
AdviceChangeValue = str | int | float | None


def _strict_iso_date(value: object) -> date:
    if isinstance(value, datetime):
        raise ValueError("date must use YYYY-MM-DD")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


ISODate = Annotated[date, BeforeValidator(_strict_iso_date)]


class UserInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertRuleInput(UserInputModel):
    symbol: str
    condition_type: str
    threshold: FiniteFloat
    name: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=160)
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)


class AlertRuleUpdate(UserInputModel):
    name: str | None = Field(default=None, max_length=40)
    condition_type: str | None = None
    threshold: FiniteFloat | None = None
    note: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=30, le=86400)


class AlertRuleItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    stock_name: str
    name: str
    condition_type: str
    condition_label: str
    threshold: float
    note: str | None = None
    enabled: bool = True
    last_checked_at: str | None = None
    last_triggered_at: str | None = None
    last_state: str = "等待"
    trigger_count: int = 0
    cooldown_seconds: int = 300
    created_at: str
    updated_at: str


class AlertEventItem(BaseModel):
    id: int
    rule_id: int
    symbol: str
    code: str
    market: str
    stock_name: str
    name: str
    condition_type: str
    event_type: str = "触发"
    message: str
    price: float
    change_pct: float
    threshold: float
    created_at: str


class AlertEvaluationItem(BaseModel):
    rule: AlertRuleItem
    current_value: float | None = None
    triggered: bool
    message: str
    event: AlertEventItem | None = None
    status: Literal["evaluated", "failed"] = "evaluated"


class AlertEvaluationSummary(BaseModel):
    checked_at: str
    checked_count: int
    triggered_count: int
    new_event_count: int
    failed_count: int = 0
    items: list[AlertEvaluationItem]


class StockNoteInput(UserInputModel):
    symbol: str
    content: str = Field(min_length=1, max_length=500)
    note_type: str = Field(default="观察", max_length=20)
    price: FiniteFloat | None = None
    trade_date: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=20)
    visible: bool = True


class StockNoteUpdate(UserInputModel):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    note_type: str | None = Field(default=None, max_length=20)
    price: FiniteFloat | None = None
    trade_date: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=20)
    visible: bool | None = None


class StockNoteItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    name: str
    note_type: str
    content: str
    price: float | None = None
    trade_date: str | None = None
    color: str | None = None
    visible: bool = True
    created_at: str
    updated_at: str


class ChartMarkItem(BaseModel):
    date: str
    kline_date: str | None = None
    price: float | None = None
    label: str
    category: str
    level: str
    description: str
    source: str
    color: str | None = None
    anchor_price_type: str = "close"
    visible: bool = True


class ChartMarkSummary(BaseModel):
    symbol: str
    updated_at: str
    marks: list[ChartMarkItem]
    categories: list[str] = Field(default_factory=list)


class WatchlistInput(UserInputModel):
    symbol: str = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=80)
    group_name: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None
    research_status: ResearchStatus | None = None
    priority: WatchlistPriority | None = None
    next_review_date: ISODate | None = None


class WatchlistUpdate(UserInputModel):
    note: str | None = Field(default=None, max_length=80)
    group_name: str | None = Field(default=None, max_length=20)
    pinned: bool = False
    research_status: ResearchStatus = "watching"
    priority: WatchlistPriority = "medium"
    next_review_date: ISODate | None = None
    unread_change_count: int = Field(default=0, ge=0)


class WatchlistMarkViewed(UserInputModel):
    clear_unread: bool = True
    viewed_through_advice_id: int | None = Field(default=None, ge=1, strict=True)


class WatchlistItem(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    note: str | None = Field(default=None, max_length=80)
    group_name: str = Field(default="默认", max_length=20)
    pinned: bool = False
    research_status: ResearchStatus = "watching"
    priority: WatchlistPriority = "medium"
    next_review_date: date | None = None
    last_viewed_at: str | None = Field(default=None, max_length=19)
    unread_change_count: int = Field(default=0, ge=0)
    latest_price: float | None = None
    latest_change_pct: float | None = None
    latest_source: str | None = None
    latest_at: str | None = None
    created_at: str
    updated_at: str


class AdviceHistoryItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    name: str
    action: str
    confidence: int
    trend_score: int
    trend_label: str
    risk_level: str
    price: float
    change_pct: float
    support: float
    resistance: float
    data_quality_score: int
    data_quality_level: str
    reason: str
    summary: str
    created_at: str
    updated_at: str | None = None
    repeat_count: int = 1
    kline_adjustment_mode: KlineAdjustmentMode = "unknown"
    kline_anchor_date: str | None = None
    kline_anchor_close: float | None = None
    kline_data_version: str = "unknown"
    kline_contract_version: str = "unknown"


class AdviceTimelineChange(BaseModel):
    category: AdviceChangeCategory
    field: str
    before: AdviceChangeValue = None
    after: AdviceChangeValue = None
    delta: int | float | None = None
    direction: AdviceChangeDirection
    comparable: bool


class AdviceTimelineItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    name: str
    action: str | None = None
    confidence: int | None = None
    trend_score: int | None = None
    trend_label: str | None = None
    risk_level: str | None = None
    price: float | None = None
    change_pct: float | None = None
    support: float | None = None
    resistance: float | None = None
    data_quality_score: int | None = None
    data_quality_level: str | None = None
    data_quality_source: str | None = None
    reason: str | None = None
    summary: str | None = None
    created_at: str
    updated_at: str | None = None
    repeat_count: int = 1
    snapshot_contract_version: str = "legacy"
    conclusion_basis: str = "legacy_unknown"
    rule_version: str = "unknown"
    model_version: str = "unknown"
    market_time: str | None = None
    kline_adjustment_mode: KlineAdjustmentMode = "unknown"
    kline_anchor_date: str | None = None
    kline_anchor_close: float | None = None
    kline_data_version: str = "unknown"
    kline_contract_version: str = "unknown"
    previous_id: int | None = None
    comparison_status: AdviceComparisonStatus = "no_previous"
    has_changes: bool = False
    changes: list[AdviceTimelineChange] = Field(default_factory=list)
