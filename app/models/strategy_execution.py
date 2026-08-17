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
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_seal_origin: Literal["publication", "legacy_backfill"]
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
    replacement_attempt_count: int = Field(default=0, ge=0)
    pool_exhausted: bool = False
    underinvested_reason: str | None = None
    notes: list[str]

    @model_validator(mode="after")
    def validate_summary_counts(self) -> Self:
        if self.evaluated_count != self.selected_count + self.rejected_count + self.unfilled_count:
            raise ValueError("组合摘要状态计数不能覆盖全部候选")
        if self.adjusted_count > self.selected_count:
            raise ValueError("约束调整数量不能大于入选数量")
        if self.eligible_count < self.selected_count or self.eligible_count > self.evaluated_count:
            raise ValueError("组合摘要可用候选数量与入选/总数不一致")
        if self.evidence_verified_count > self.evaluated_count:
            raise ValueError("组合摘要时点证据数量不能大于候选总数")
        if self.no_trade != (self.status == "no_trade") or self.no_trade != (self.selected_count == 0):
            raise ValueError("组合摘要 no_trade、状态与入选数量不一致")
        if self.no_trade and not self.no_trade_reasons:
            raise ValueError("无交易组合必须说明原因")
        return self


class PortfolioDraft(BaseModel):
    context: StrategyExecutionContext
    summary: PortfolioDraftSummary
    selected: list[PortfolioCandidate]
    candidate_preview: list[PortfolioCandidate]
    candidate_total: int = Field(ge=0)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_draft_binding(self) -> Self:
        if self.context.status != self.summary.status:
            raise ValueError("策略执行上下文状态与组合摘要不一致")
        if len(self.selected) != self.summary.selected_count:
            raise ValueError("策略执行入选列表数量与摘要不一致")
        if self.candidate_total != self.summary.evaluated_count:
            raise ValueError("策略执行候选总数与摘要不一致")
        if len(self.candidate_preview) > min(100, self.candidate_total):
            raise ValueError("策略执行候选预览数量超出边界")
        selected_symbols = [item.symbol for item in self.selected]
        preview_symbols = [item.symbol for item in self.candidate_preview]
        if len(selected_symbols) != len(set(selected_symbols)):
            raise ValueError("策略执行入选股票不能重复")
        if len(preview_symbols) != len(set(preview_symbols)):
            raise ValueError("策略执行候选预览股票不能重复")
        if any(item.status not in {"selected", "constraint_adjusted"} for item in self.selected):
            raise ValueError("策略执行入选列表包含非入选状态")
        return self


class PortfolioCandidatePage(BaseModel):
    execution_id: int = Field(ge=1)
    items: list[PortfolioCandidate]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_shape(self) -> Self:
        expected = (self.total + self.page_size - 1) // self.page_size if self.total else 0
        if self.page_count != expected or len(self.items) > min(self.page_size, self.total):
            raise ValueError("策略候选分页计数不一致")
        if len({item.symbol for item in self.items}) != len(self.items):
            raise ValueError("策略候选分页不能包含重复股票")
        return self


class StrategyExecutionPage(BaseModel):
    items: list[StrategyExecutionContext]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_shape(self) -> Self:
        expected = (self.total + self.page_size - 1) // self.page_size if self.total else 0
        if self.page_count != expected or len(self.items) > min(self.page_size, self.total):
            raise ValueError("策略执行分页计数不一致")
        if len({item.execution_id for item in self.items}) != len(self.items):
            raise ValueError("策略执行分页不能包含重复执行")
        return self


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
