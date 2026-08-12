"""Contracts for point-in-time StrategySpec execution and portfolio drafts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrategyExecutionKind = Literal["latest_scan", "historical_replay"]
StrategyExecutionMarketScanMode = Literal["official", "intraday"]
StrategyExecutionStatus = Literal["ready", "no_trade", "blocked"]
PortfolioCandidateStatus = Literal[
    "selected",
    "rejected",
    "constraint_adjusted",
    "unfilled",
]
PortfolioCandidateSort = Literal[
    "utility_score",
    "alpha_1d",
    "alpha_5d",
    "alpha_20d",
    "confidence",
    "risk",
    "tradability",
    "original_rank",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class StrategyExecutionRequest(_StrictModel):
    strategy_id: int = Field(ge=1)
    revision: int | None = Field(default=None, ge=1)
    kind: StrategyExecutionKind = "latest_scan"
    run_id: int | None = Field(default=None, ge=1)
    data_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Strategy execution remains isolated from the read-only pre-open review
    # cohort.  Supporting pre-open signals here requires a separate strategy
    # evidence contract and database migration, not an implicit enum expansion.
    mode: StrategyExecutionMarketScanMode = "official"
    notional_cash_cny: float = Field(
        default=1_000_000.0,
        ge=10_000,
        le=1_000_000_000,
        allow_inf_nan=False,
    )
    current_weights: dict[str, float] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def validate_execution_target(self) -> Self:
        if self.run_id is not None and self.data_date is not None:
            raise ValueError("run_id 与 data_date 只能选择一个")
        if self.kind == "latest_scan" and (self.run_id is not None or self.data_date is not None):
            raise ValueError("latest_scan 不接受历史批次或日期")
        if self.kind == "historical_replay" and self.run_id is None and self.data_date is None:
            raise ValueError("historical_replay 必须指定 run_id 或 data_date")
        if any(weight < 0 or weight > 1 for weight in self.current_weights.values()):
            raise ValueError("当前持仓权重必须位于 [0, 1] 区间")
        if sum(self.current_weights.values()) > 1.0000001:
            raise ValueError("当前持仓权重合计不能超过 1")
        return self


class StrategyExecutionContext(BaseModel):
    execution_id: int = Field(ge=1)
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    strategy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: StrategyExecutionKind
    market_scan_run_id: int = Field(ge=1)
    rule_version: str
    data_as_of: str
    data_date: str
    cost_rule_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    point_in_time: Literal[True] = True
    status: StrategyExecutionStatus
    created_at: str


class PortfolioCandidate(BaseModel):
    symbol: str
    code: str
    name: str
    board: str
    board_label: str
    industry: str | None = None
    original_rank: int | None = Field(default=None, ge=1)
    utility_rank: int | None = Field(default=None, ge=1)
    utility_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    alpha_1d: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    alpha_5d: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    alpha_20d: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    confidence: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    risk: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    tradability: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pareto_front: bool = False
    status: PortfolioCandidateStatus
    target_weight: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    target_quantity: int = Field(default=0, ge=0)
    estimated_gross_amount_cny: float = Field(default=0, ge=0, allow_inf_nan=False)
    estimated_round_trip_cost_cny: float = Field(default=0, ge=0, allow_inf_nan=False)
    evidence_verified: bool
    evidence_freshness: str
    hard_filter_failures: list[str] = Field(default_factory=list)
    marginal_contributions: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    minimum_changes: list[str] = Field(default_factory=list)
    rank_sensitivity: dict[str, int] = Field(default_factory=dict)
    rank_change_reason: str


class PortfolioDraftSummary(BaseModel):
    status: StrategyExecutionStatus
    no_trade: bool
    no_trade_reasons: list[str]
    evaluated_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    adjusted_count: int = Field(ge=0)
    unfilled_count: int = Field(ge=0)
    target_invested_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    estimated_turnover: float = Field(ge=0, le=2, allow_inf_nan=False)
    estimated_round_trip_cost_cny: float = Field(ge=0, allow_inf_nan=False)
    residual_cash_cny: float = Field(ge=0, allow_inf_nan=False)
    evidence_verified_count: int = Field(ge=0)
    notes: list[str]


class PortfolioDraft(BaseModel):
    context: StrategyExecutionContext
    summary: PortfolioDraftSummary
    selected: list[PortfolioCandidate]
    candidate_preview: list[PortfolioCandidate]
    candidate_total: int = Field(ge=0)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PortfolioCandidatePage(BaseModel):
    execution_id: int = Field(ge=1)
    items: list[PortfolioCandidate]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)


class StrategyExecutionPage(BaseModel):
    items: list[StrategyExecutionContext]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)


class StrategyExecutionCandidateChange(BaseModel):
    symbol: str
    name: str
    left_rank: int | None = Field(default=None, ge=1)
    right_rank: int | None = Field(default=None, ge=1)
    left_weight: float = Field(ge=0, le=1)
    right_weight: float = Field(ge=0, le=1)


class StrategyExecutionComparison(BaseModel):
    left: StrategyExecutionContext
    right: StrategyExecutionContext
    same_strategy_fingerprint: bool
    same_rule_version: bool
    added: list[StrategyExecutionCandidateChange]
    removed: list[StrategyExecutionCandidateChange]
    retained_changed: list[StrategyExecutionCandidateChange]


__all__ = [
    "PortfolioCandidate",
    "PortfolioCandidatePage",
    "PortfolioCandidateStatus",
    "PortfolioCandidateSort",
    "PortfolioDraft",
    "PortfolioDraftSummary",
    "StrategyExecutionContext",
    "StrategyExecutionPage",
    "StrategyExecutionCandidateChange",
    "StrategyExecutionComparison",
    "StrategyExecutionMarketScanMode",
    "StrategyExecutionRequest",
]
