"""Local paper-trading contracts for replaying frozen review plans."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, model_validator


PositiveMoney = Annotated[FiniteFloat, Field(gt=0)]
CostProfileName = Literal["base", "conservative", "stress"]
PaperStrategyStatus = Literal["pending", "open", "closed", "skipped", "expired", "data_unavailable"]
PaperTradeSide = Literal["buy", "sell"]
PaperEventCategory = Literal["lifecycle", "execution", "risk", "data", "rule", "cost"]
PaperEventSeverity = Literal["info", "warning", "critical"]
PaperRuleQuality = Literal["ok", "degraded"]
PaperMetricStatus = Literal["available", "unavailable"]
PaperExitReason = Literal[
    "target_hit",
    "stop_hit",
    "target_stop_ambiguous",
    "horizon_close",
    "target_before_entry",
    "invalid_before_entry",
    "entry_expired",
    "t1_deferred_target",
    "t1_deferred_stop",
    "t1_deferred_ambiguous",
]


class PaperTradingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperCostOverrides(PaperTradingInput):
    commission_rate_pct: FiniteFloat | None = Field(default=None, ge=0, le=5)
    minimum_commission: FiniteFloat | None = Field(default=None, ge=0, le=10_000)
    stamp_duty_sell_pct: FiniteFloat | None = Field(default=None, ge=0, le=5)
    transfer_fee_pct: FiniteFloat | None = Field(default=None, ge=0, le=5)
    slippage_buy_pct: FiniteFloat | None = Field(default=None, ge=0, le=10)
    slippage_sell_pct: FiniteFloat | None = Field(default=None, ge=0, le=10)


class PaperStrategyCreate(PaperTradingInput):
    plan_id: int = Field(gt=0)
    expected_plan_revision: int = Field(gt=0)
    expected_plan_payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocation_pct: FiniteFloat = Field(default=10, ge=1, le=100)
    priority: int = Field(default=0, ge=-1000, le=1000)
    entry_expiry_sessions: int = Field(default=5, ge=1, le=60)


class PaperTradingAccountUpdate(PaperTradingInput):
    initial_cash: PositiveMoney | None = Field(default=None, ge=10_000, le=1_000_000_000)
    default_cost_profile: CostProfileName | None = None

    @model_validator(mode="after")
    def _require_change(self) -> PaperTradingAccountUpdate:
        if self.initial_cash is None and self.default_cost_profile is None:
            raise ValueError("至少需要修改一个模拟账户字段")
        return self


class PaperSimulationRequest(PaperTradingInput):
    as_of: datetime | None = None
    cost_profile: CostProfileName | None = None
    cost_overrides: PaperCostOverrides | None = None
    benchmark_symbol: str | None = Field(default="000300.SH", max_length=24)


class PaperTradingAccount(BaseModel):
    id: int = 1
    name: str
    initial_cash: float
    modelled_one_way_friction_pct: float = 0
    default_cost_profile: CostProfileName = "base"
    created_at: str
    updated_at: str


class PaperCostProfile(BaseModel):
    profile_id: str
    name: str
    version: str
    effective_from: str
    commission_rate_pct: float
    minimum_commission: float
    stamp_duty_sell_pct: float
    transfer_fee_pct: float
    slippage_buy_pct: float
    slippage_sell_pct: float
    source_urls: list[str] = Field(default_factory=list)
    note: str


class PaperTradeRuleProfile(BaseModel):
    profile_id: str
    exchange: str
    board: str
    effective_from: str
    price_limit_pct: float | None = None
    min_buy_quantity: int
    buy_quantity_step: int
    first_listing_sessions_without_limit: int = 0
    source_url: str
    quality: PaperRuleQuality = "ok"
    degradation_reasons: list[str] = Field(default_factory=list)


class PaperInstrumentMetadata(BaseModel):
    symbol: str
    name: str | None = None
    market: str | None = None
    list_date: str | None = None
    is_st: bool | None = None
    source: str | None = None
    status_effective_date: str | None = None


class PaperStrategy(BaseModel):
    id: int
    plan_id: int
    plan_revision: int
    plan_payload_digest: str = Field(pattern=r"^(?:[0-9a-f]{64}|legacy-unverified)$")
    advice_id: int
    symbol: str
    activation_market_time: str
    allocation_pct: float
    priority: int = 0
    entry_expiry_sessions: int = 5
    snapshot_market_time: str
    snapshot_price: float
    snapshot_adjustment_mode: str
    snapshot_anchor_date: str | None = None
    snapshot_anchor_close: float | None = None
    snapshot_data_version: str
    snapshot_contract_version: str
    target_price: float
    stop_price: float
    horizon_days: int
    status: PaperStrategyStatus
    allocation_order: int | None = None
    normalized_target_price: float | None = None
    normalized_stop_price: float | None = None
    entry_wait_sessions: int = 0
    entry_date: str | None = None
    entry_price: float | None = None
    quantity: int = 0
    buy_friction: float = 0
    held_sessions: int = 0
    last_price: float | None = None
    pending_exit_reason: str | None = None
    pending_exit_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    sell_friction: float = 0
    gross_realized_pnl: float | None = None
    realized_pnl: float | None = None
    return_pct: float | None = None
    rule_profile_id: str | None = None
    rule_data_degraded: bool = False
    error_message: str | None = None
    last_processed_date: str | None = None
    created_at: str
    updated_at: str


class PaperPosition(BaseModel):
    strategy_id: int
    symbol: str
    quantity: int
    entry_date: str
    entry_price: float
    cost_basis: float
    last_price: float
    market_value: float
    estimated_exit_friction: float
    unrealized_pnl: float
    return_pct: float
    target_price: float
    stop_price: float
    held_sessions: int
    pending_exit_reason: str | None = None


class PaperTrade(BaseModel):
    id: int
    run_id: int
    strategy_id: int
    symbol: str
    side: PaperTradeSide
    trade_date: str
    price: float
    quantity: int
    gross_amount: float
    commission_amount: float = 0
    stamp_duty_amount: float = 0
    transfer_fee_amount: float = 0
    slippage_amount: float = 0
    friction_amount: float
    reason: str
    created_at: str


class PaperTradingEvent(BaseModel):
    id: int
    run_id: int
    sequence: int
    strategy_id: int | None = None
    symbol: str | None = None
    event_date: str
    event_code: str
    category: PaperEventCategory
    severity: PaperEventSeverity
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: str


class PaperEquityPoint(BaseModel):
    id: int
    run_id: int
    as_of_date: str
    cash_balance: float
    market_value: float
    estimated_exit_friction: float
    total_equity: float
    gross_equity: float
    cumulative_cost: float
    realized_pnl: float
    unrealized_pnl: float
    return_pct: float
    gross_return_pct: float
    benchmark_equity: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    exposure_pct: float
    drawdown_pct: float
    created_at: str


class PaperTradingPerformance(BaseModel):
    strategy_count: int
    pending_count: int
    open_count: int
    closed_count: int
    skipped_count: int
    expired_count: int = 0
    data_unavailable_count: int
    win_count: int
    win_rate_pct: float | None = None
    cash_balance: float
    market_value: float
    total_equity: float
    gross_equity: float = 0
    realized_pnl: float
    unrealized_pnl: float
    gross_pnl: float = 0
    total_cost: float = 0
    cost_drag_pct: float = 0
    cost_to_gross_profit_pct: float | None = None
    total_return_pct: float
    gross_return_pct: float = 0
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    max_drawdown_pct: float
    max_drawdown_duration_sessions: int = 0
    recovery_duration_sessions: int | None = None
    average_win: float | None = None
    average_loss: float | None = None
    payoff_ratio: float | None = None
    expectancy: float | None = None
    profit_factor: float | None = None
    turnover_pct: float = 0
    average_exposure_pct: float = 0
    maximum_exposure_pct: float = 0
    return_observation_count: int = 0
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    risk_metric_status: PaperMetricStatus = "unavailable"
    risk_metric_message: str = "收益观察样本不足"
    sample_warning: str | None = None


class PaperTradingRun(BaseModel):
    id: int
    as_of: str
    rule_version: str
    modelled_one_way_friction_pct: float = 0
    cost_profile_id: str = "legacy"
    cost_profile_name: str = "legacy"
    cost_profile_version: str = "legacy"
    benchmark_symbol: str | None = None
    benchmark_status: str = "unavailable"
    benchmark_message: str | None = None
    strategy_count: int
    execution_count: int
    closed_count: int
    data_unavailable_count: int
    input_fingerprint: str = ""
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_snapshot_hash: str = ""
    market_data_hash: str = ""
    data_start_date: str | None = None
    data_end_date: str | None = None
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    rule_profiles: list[dict[str, JsonValue]] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    message: str
    created_at: str


class PaperTradingDashboard(BaseModel):
    account: PaperTradingAccount
    performance: PaperTradingPerformance
    strategies: list[PaperStrategy] = Field(default_factory=list)
    positions: list[PaperPosition] = Field(default_factory=list)
    trades: list[PaperTrade] = Field(default_factory=list)
    events: list[PaperTradingEvent] = Field(default_factory=list)
    equity_curve: list[PaperEquityPoint] = Field(default_factory=list)
    latest_run: PaperTradingRun | None = None
    selected_run_id: int | None = None
    runs: list[PaperTradingRun] = Field(default_factory=list)
    cost_profiles: list[PaperCostProfile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PaperSimulationSummary(BaseModel):
    run_id: int | None = None
    as_of: str
    execution_count: int
    closed_count: int
    data_unavailable_count: int
    dashboard: PaperTradingDashboard


class PaperRunComparison(BaseModel):
    left_run: PaperTradingRun
    right_run: PaperTradingRun
    left_performance: PaperTradingPerformance
    right_performance: PaperTradingPerformance
    deltas: dict[str, float | None]


class PaperRunExport(BaseModel):
    run: PaperTradingRun
    performance: PaperTradingPerformance
    strategies: list[PaperStrategy]
    trades: list[PaperTrade]
    events: list[PaperTradingEvent]
    equity_curve: list[PaperEquityPoint]


class PaperStrategySimulation(BaseModel):
    strategy_id: int
    status: PaperStrategyStatus
    allocation_order: int | None = None
    normalized_target_price: float | None = None
    normalized_stop_price: float | None = None
    entry_wait_sessions: int = 0
    entry_date: str | None = None
    entry_price: float | None = None
    quantity: int = 0
    buy_friction: float = 0
    held_sessions: int = 0
    last_price: float | None = None
    pending_exit_reason: str | None = None
    pending_exit_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    sell_friction: float = 0
    gross_realized_pnl: float | None = None
    realized_pnl: float | None = None
    return_pct: float | None = None
    rule_profile_id: str | None = None
    rule_data_degraded: bool = False
    error_message: str | None = None
    last_processed_date: str | None = None


class PaperTradeDraft(BaseModel):
    strategy_id: int
    symbol: str
    side: PaperTradeSide
    trade_date: str
    price: float
    quantity: int
    gross_amount: float
    commission_amount: float = 0
    stamp_duty_amount: float = 0
    transfer_fee_amount: float = 0
    slippage_amount: float = 0
    friction_amount: float
    reason: str


class PaperTradingEventDraft(BaseModel):
    sequence: int
    strategy_id: int | None = None
    symbol: str | None = None
    event_date: str
    event_code: str
    category: PaperEventCategory
    severity: PaperEventSeverity
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class PaperEquityPointDraft(BaseModel):
    as_of_date: str
    cash_balance: float
    market_value: float
    estimated_exit_friction: float
    total_equity: float
    gross_equity: float
    cumulative_cost: float
    realized_pnl: float
    unrealized_pnl: float
    return_pct: float
    gross_return_pct: float
    benchmark_equity: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    exposure_pct: float
    drawdown_pct: float


class PaperSimulationDraft(BaseModel):
    as_of: str
    strategy_ids: list[int]
    strategies: list[PaperStrategySimulation]
    trades: list[PaperTradeDraft]
    events: list[PaperTradingEventDraft]
    equity_curve: list[PaperEquityPointDraft]
    cost_profile: PaperCostProfile
    rule_profiles: list[PaperTradeRuleProfile]
    benchmark_symbol: str | None = None
    benchmark_status: str = "unavailable"
    benchmark_message: str | None = None
    input_fingerprint: str
    strategy_snapshot_hash: str
    market_data_hash: str
    data_start_date: str | None = None
    data_end_date: str | None = None
    data_sources: list[str] = Field(default_factory=list)
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    execution_count: int
    closed_count: int
    data_unavailable_count: int
    message: str


__all__ = [
    "CostProfileName",
    "PaperCostOverrides",
    "PaperCostProfile",
    "PaperEquityPoint",
    "PaperEquityPointDraft",
    "PaperEventCategory",
    "PaperEventSeverity",
    "PaperExitReason",
    "PaperInstrumentMetadata",
    "PaperPosition",
    "PaperRunComparison",
    "PaperRunExport",
    "PaperSimulationDraft",
    "PaperSimulationRequest",
    "PaperSimulationSummary",
    "PaperStrategy",
    "PaperStrategyCreate",
    "PaperStrategySimulation",
    "PaperStrategyStatus",
    "PaperTrade",
    "PaperTradeDraft",
    "PaperTradeRuleProfile",
    "PaperTradeSide",
    "PaperTradingAccount",
    "PaperTradingAccountUpdate",
    "PaperTradingDashboard",
    "PaperTradingEvent",
    "PaperTradingEventDraft",
    "PaperTradingPerformance",
    "PaperTradingRun",
]
