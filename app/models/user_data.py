"""User-managed watchlist, alert, note, chart mark, and advice-history models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


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


class AlertEvaluationSummary(BaseModel):
    checked_at: str
    checked_count: int
    triggered_count: int
    new_event_count: int
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
    symbol: str
    note: str | None = Field(default=None, max_length=80)
    group_name: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None


class WatchlistItem(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    note: str | None = None
    group_name: str = "默认"
    pinned: bool = False
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
