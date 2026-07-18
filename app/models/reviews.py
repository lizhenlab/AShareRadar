"""Structured advice review plans, evaluations, and bounded scan contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.market import KlineAdjustmentMode


PositiveFiniteFloat = Annotated[FiniteFloat, Field(gt=0)]
AdviceReviewStatus = Literal["pending", "insufficient", "evaluated"]
AdviceReviewConclusion = Literal[
    "pending",
    "insufficient_data",
    "target_hit",
    "stop_hit",
    "target_stop_ambiguous",
    "horizon_gain",
    "horizon_loss",
    "horizon_flat",
]
WatchlistScanUniverse = Literal["watchlist", "symbols"]
WatchlistScanCondition = Literal[
    "close_above_ma20",
    "close_below_ma20",
    "breakout_20d_high",
    "volume_surge_5d",
]


class ReviewInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdviceReviewPlanInput(ReviewInputModel):
    advice_id: int = Field(gt=0)
    symbol: str = Field(min_length=1, max_length=20)
    hypothesis: str = Field(min_length=1, max_length=1000)
    trigger_condition: str = Field(min_length=1, max_length=1000)
    invalidation_condition: str = Field(min_length=1, max_length=1000)
    target_price: PositiveFiniteFloat
    stop_price: PositiveFiniteFloat
    horizon_days: int = Field(ge=1, le=60)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("hypothesis", "trigger_condition", "invalidation_condition")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("evidence_refs")
    @classmethod
    def clean_evidence_refs(cls, value: list[str]) -> list[str]:
        return _clean_evidence_refs(value)

    @model_validator(mode="after")
    def validate_price_order(self) -> AdviceReviewPlanInput:
        if self.target_price <= self.stop_price:
            raise ValueError("目标价必须高于止损价")
        return self


class AdviceReviewPlanUpdate(ReviewInputModel):
    hypothesis: str | None = Field(default=None, min_length=1, max_length=1000)
    trigger_condition: str | None = Field(default=None, min_length=1, max_length=1000)
    invalidation_condition: str | None = Field(default=None, min_length=1, max_length=1000)
    target_price: PositiveFiniteFloat | None = None
    stop_price: PositiveFiniteFloat | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    evidence_refs: list[str] | None = Field(default=None, max_length=50)

    @field_validator("hypothesis", "trigger_condition", "invalidation_condition")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return _required_text(value) if value is not None else None

    @field_validator("evidence_refs")
    @classmethod
    def clean_optional_evidence_refs(cls, value: list[str] | None) -> list[str] | None:
        return _clean_evidence_refs(value) if value is not None else None


class AdviceSnapshotRef(BaseModel):
    advice_id: int
    symbol: str
    market_time: str
    price: float
    adjustment_mode: KlineAdjustmentMode = "unknown"
    anchor_date: str | None = None
    anchor_close: float | None = None
    data_version: str = "unknown"
    contract_version: str = "unknown"


class AdviceReviewPlan(BaseModel):
    id: int
    advice_id: int
    symbol: str
    snapshot_market_time: str
    snapshot_price: float
    snapshot_adjustment_mode: KlineAdjustmentMode = "unknown"
    snapshot_anchor_date: str | None = None
    snapshot_anchor_close: float | None = None
    snapshot_data_version: str = "unknown"
    snapshot_contract_version: str = "unknown"
    hypothesis: str
    trigger_condition: str
    invalidation_condition: str
    target_price: float
    stop_price: float
    horizon_days: int
    evidence_refs: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class AdviceReviewEvaluationRequest(ReviewInputModel):
    as_of: datetime | None = None


class AdviceReviewEvaluationDraft(BaseModel):
    plan_id: int
    plan_revision: int
    advice_id: int
    symbol: str
    snapshot_market_time: str
    as_of: str
    evaluated_at: str
    status: AdviceReviewStatus
    conclusion: AdviceReviewConclusion
    rule_version: str
    snapshot_adjustment_mode: KlineAdjustmentMode = "unknown"
    snapshot_anchor_date: str | None = None
    snapshot_anchor_close: float | None = None
    snapshot_data_version: str = "unknown"
    snapshot_contract_version: str = "unknown"
    evaluation_adjustment_mode: KlineAdjustmentMode = "unknown"
    evaluation_data_version: str = "unknown"
    evaluation_contract_version: str = "unknown"
    anchor_evaluation_close: float | None = None
    price_scale_factor: float | None = None
    normalized_entry_price: float | None = None
    normalized_target_price: float | None = None
    normalized_stop_price: float | None = None
    entry_price: float
    target_price: float
    stop_price: float
    horizon_days: int
    visible_bar_count: int = Field(ge=0)
    visible_start_date: str | None = None
    visible_end_date: str | None = None
    available_forward_days: int = Field(ge=0)
    forward_start_date: str | None = None
    forward_end_date: str | None = None
    return_pct: float | None = None
    max_favorable_excursion_pct: float | None = None
    max_adverse_excursion_pct: float | None = None
    target_hit: bool = False
    target_hit_date: str | None = None
    stop_hit: bool = False
    stop_hit_date: str | None = None


class AdviceReviewEvaluation(AdviceReviewEvaluationDraft):
    id: int


class AdviceReviewDetail(BaseModel):
    plan: AdviceReviewPlan
    latest_evaluation: AdviceReviewEvaluation | None = None


class WatchlistScanRequest(ReviewInputModel):
    universe: WatchlistScanUniverse = "watchlist"
    symbols: list[str] = Field(default_factory=list, max_length=50)
    conditions: list[WatchlistScanCondition] = Field(min_length=1, max_length=4)
    rule_version: Literal["watchlist-scan-v1"] = "watchlist-scan-v1"
    as_of: datetime | None = None


class WatchlistScanItem(BaseModel):
    symbol: str
    data_date: str
    matched: bool
    condition_results: dict[str, bool]
    matched_conditions: list[WatchlistScanCondition]
    metrics: dict[str, float]


class WatchlistScanMissing(BaseModel):
    symbol: str
    reason: str


class WatchlistScanResponse(BaseModel):
    universe: list[str]
    success: list[WatchlistScanItem]
    missing: list[WatchlistScanMissing]
    as_of: str
    rule_version: Literal["watchlist-scan-v1"] = "watchlist-scan-v1"
    conditions: list[WatchlistScanCondition]


def _required_text(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError("内容不能为空")
    return cleaned


def _clean_evidence_refs(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(value.split()).strip()
        if not item:
            raise ValueError("证据引用不能为空")
        if len(item) > 240:
            raise ValueError("单条证据引用不能超过240个字符")
        if item not in seen:
            cleaned.append(item)
            seen.add(item)
    return cleaned


__all__ = [
    "AdviceReviewConclusion",
    "AdviceReviewDetail",
    "AdviceReviewEvaluation",
    "AdviceReviewEvaluationDraft",
    "AdviceReviewEvaluationRequest",
    "AdviceReviewPlan",
    "AdviceReviewPlanInput",
    "AdviceReviewPlanUpdate",
    "AdviceReviewStatus",
    "AdviceSnapshotRef",
    "WatchlistScanCondition",
    "WatchlistScanItem",
    "WatchlistScanMissing",
    "WatchlistScanRequest",
    "WatchlistScanResponse",
    "WatchlistScanUniverse",
]
