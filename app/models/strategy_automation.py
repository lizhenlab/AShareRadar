"""Contracts for version-pinned strategy schedules, alerts and simulation plans."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


StrategyAlertType = Literal["new_entry", "removed", "utility_cross", "data_stale", "evidence_invalid"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class StrategyAlertCondition(_StrictModel):
    event_type: StrategyAlertType
    utility_threshold: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_threshold(self) -> Self:
        if self.event_type == "utility_cross" and self.utility_threshold is None:
            raise ValueError("评分跨阈值提醒必须提供 utility_threshold")
        if self.event_type != "utility_cross" and self.utility_threshold is not None:
            raise ValueError("只有评分跨阈值提醒可以提供 utility_threshold")
        return self


class StrategyScheduleCreate(_StrictModel):
    strategy_id: int = Field(ge=1)
    revision: int | None = Field(default=None, ge=1)
    cadence: Literal["daily_after_close", "trading_day_intraday"] = "daily_after_close"
    mode: Literal["official", "intraday"] = "official"
    notional_cash_cny: float = Field(default=1_000_000, ge=10_000, le=1_000_000_000, allow_inf_nan=False)
    alert_conditions: list[StrategyAlertCondition] = Field(
        default_factory=lambda: [
            StrategyAlertCondition(event_type="new_entry"),
            StrategyAlertCondition(event_type="removed"),
            StrategyAlertCondition(event_type="data_stale"),
            StrategyAlertCondition(event_type="evidence_invalid"),
        ],
        max_length=8,
    )

    @model_validator(mode="after")
    def validate_mode_matches_cadence(self) -> Self:
        expected = "official" if self.cadence == "daily_after_close" else "intraday"
        if self.mode != expected:
            raise ValueError(f"{self.cadence} 只能使用 {expected} 扫描")
        if len({(item.event_type, item.utility_threshold) for item in self.alert_conditions}) != len(
            self.alert_conditions
        ):
            raise ValueError("提醒条件不能重复")
        return self


class StrategyScheduleUpdate(_StrictModel):
    enabled: bool


class StrategySchedule(BaseModel):
    schedule_id: int = Field(ge=1)
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    strategy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cadence: Literal["daily_after_close", "trading_day_intraday"]
    mode: Literal["official", "intraday"]
    notional_cash_cny: float
    alert_conditions: list[StrategyAlertCondition]
    enabled: bool
    last_execution_id: int | None = Field(default=None, ge=1)
    last_market_scan_run_id: int | None = Field(default=None, ge=1)
    created_at: str
    updated_at: str


class StrategySchedulePage(BaseModel):
    items: list[StrategySchedule]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)


class StrategyAlertEvent(BaseModel):
    event_id: int = Field(ge=1)
    schedule_id: int = Field(ge=1)
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    strategy_fingerprint: str
    execution_id: int = Field(ge=1)
    execution_fingerprint: str
    data_as_of: str
    event_type: StrategyAlertType
    symbol: str | None = None
    message: str
    trigger: dict[str, object]
    created_at: str


class StrategyAlertEventPage(BaseModel):
    items: list[StrategyAlertEvent]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)


class StrategyAutomationRunSummary(BaseModel):
    checked_count: int = Field(ge=0)
    executed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list, max_length=20)


class StrategySimulationOrder(BaseModel):
    symbol: str
    name: str
    board_label: str
    research_side: Literal["paper_buy"] = "paper_buy"
    target_weight: float = Field(ge=0, le=1)
    target_quantity: int = Field(ge=0)
    estimated_gross_amount_cny: float = Field(ge=0)
    estimated_round_trip_cost_cny: float = Field(ge=0)
    earliest_exit_policy: str
    constraint_notes: list[str]


class StrategySimulationPlan(BaseModel):
    plan_id: int = Field(ge=1)
    execution_id: int = Field(ge=1)
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    strategy_fingerprint: str
    execution_fingerprint: str
    rule_version: str
    data_as_of: str
    cost_rule_fingerprint: str
    status: Literal["draft", "no_trade"]
    orders: list[StrategySimulationOrder]
    disclaimers: list[str]
    plan_digest: str
    created_at: str


__all__ = [
    "StrategyAlertCondition",
    "StrategyAlertEvent",
    "StrategyAlertEventPage",
    "StrategyAutomationRunSummary",
    "StrategySchedule",
    "StrategyScheduleCreate",
    "StrategySchedulePage",
    "StrategyScheduleUpdate",
    "StrategySimulationOrder",
    "StrategySimulationPlan",
]
