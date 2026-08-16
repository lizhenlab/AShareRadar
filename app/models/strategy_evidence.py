"""Compact response contracts for the strategy evidence center."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EvidenceCenterStatus = Literal["insufficient_data", "blocked", "eligible_for_manual_review"]
EvidenceAvailability = Literal["available", "insufficient_data", "unavailable"]
StrategyEvidenceExecutionCompatibility = Literal[
    "not_available", "compatible", "incompatible"
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StrategyEvidenceCoverage(_StrictModel):
    scope: str
    total_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    constraint_adjusted_count: int = Field(ge=0)
    unfilled_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)


class StrategyDimensionEvidence(_StrictModel):
    dimension: Literal["alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"]
    selected_average: float | None = None
    candidate_average: float | None = None


class StrategyTopNEvidence(_StrictModel):
    top_n: int = Field(ge=1)
    horizon_trading_days: int = Field(ge=1)
    status: str
    sample_size: int = Field(ge=0)
    independent_session_count: int = Field(ge=0)
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    turnover_rate: float | None = None
    maximum_drawdown: float | None = None
    maximum_adverse_excursion: float | None = None
    confidence_interval_95: list[float] = Field(default_factory=list, max_length=2)
    insufficient_reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyRankEvidence(_StrictModel):
    horizon_trading_days: int = Field(ge=1)
    status: str
    independent_session_count: int = Field(ge=0)
    rank_ic: float | None = None
    icir: float | None = None
    confidence_interval_95: list[float] = Field(default_factory=list, max_length=2)
    decile_monotonic: bool | None = None


class StrategyEvidenceResearchBoundary(_StrictModel):
    status: Literal["shadow_only"] = "shadow_only"
    baseline_kind: Literal["offline_evaluation_baseline"] = "offline_evaluation_baseline"
    baseline_production_score_rule_version: Literal["full-market-score-v4"] = (
        "full-market-score-v4"
    )
    baseline_production_score_spec_hash: Literal[
        "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    ] = "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    execution_contract_compatibility: StrategyEvidenceExecutionCompatibility = (
        "not_available"
    )
    production_ranking_mutated: Literal[False] = False
    statement: Literal["影子研究，不改变生产排名"] = "影子研究，不改变生产排名"


class StrategyShadowCoverageEvidence(_StrictModel):
    status: EvidenceAvailability = "unavailable"
    independent_session_count: int | None = Field(default=None, ge=0)
    scored_run_count: int | None = Field(default=None, ge=0)
    scored_item_count: int | None = Field(default=None, ge=0)
    item_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowTopNEvidence(_StrictModel):
    top_n: Literal[20, 50, 100]
    horizon_trading_days: Literal[5] = 5
    status: EvidenceAvailability = "unavailable"
    sample_size: int | None = Field(default=None, ge=0)
    independent_session_count: int | None = Field(default=None, ge=0)
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    turnover_rate: float | None = Field(default=None, ge=0)
    insufficient_reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowRankDeltaEvidence(_StrictModel):
    status: EvidenceAvailability = "unavailable"
    compared_run_count: int | None = Field(default=None, ge=0)
    compared_item_count: int | None = Field(default=None, ge=0)
    candidate_ranking_count: int | None = Field(default=None, ge=0)
    production_ranking_count: int | None = Field(default=None, ge=0)
    common_symbol_count: int | None = Field(default=None, ge=0)
    missing_candidate_count: int | None = Field(default=None, ge=0)
    missing_production_count: int | None = Field(default=None, ge=0)
    mean_rank_delta: float | None = None
    median_rank_delta: float | None = None
    mean_absolute_rank_delta: float | None = Field(default=None, ge=0)
    maximum_absolute_rank_delta: float | None = Field(default=None, ge=0)
    top20_overlap_ratio: float | None = Field(default=None, ge=0, le=1)
    top50_overlap_ratio: float | None = Field(default=None, ge=0, le=1)
    top100_overlap_ratio: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowConstraintEvidence(_StrictModel):
    status: EvidenceAvailability = "unavailable"
    passed: bool | None = None
    hysteresis_turnover_rate: float | None = Field(default=None, ge=0)
    failed_constraints: list[str] = Field(default_factory=list, max_length=20)
    reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowExposureEvidence(_StrictModel):
    status: EvidenceAvailability = "unavailable"
    passed: bool | None = None
    record_count: int | None = Field(default=None, ge=0)
    maximum_absolute_share_difference: float | None = Field(default=None, ge=0, le=1)
    threshold: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowPromotionGateEvidence(_StrictModel):
    status: EvidenceAvailability = "unavailable"
    gate_version: str | None = None
    decision: str | None = None
    passed: bool | None = None
    failed_criteria: list[str] = Field(default_factory=list, max_length=30)
    reasons: list[str] = Field(default_factory=list, max_length=20)


class StrategyShadowEvidence(_StrictModel):
    candidate_id: str
    status: str
    spec_hash: str | None = None
    point_in_time_integrity_verified: bool
    independent_session_count: int = Field(ge=0)
    evidence_status: EvidenceAvailability = "unavailable"
    coverage: StrategyShadowCoverageEvidence = Field(default_factory=StrategyShadowCoverageEvidence)
    top_n: list[StrategyShadowTopNEvidence] = Field(default_factory=list, max_length=3)
    rank_delta_vs_production: StrategyShadowRankDeltaEvidence = Field(default_factory=StrategyShadowRankDeltaEvidence)
    constraints: StrategyShadowConstraintEvidence = Field(default_factory=StrategyShadowConstraintEvidence)
    exposure: StrategyShadowExposureEvidence = Field(default_factory=StrategyShadowExposureEvidence)
    promotion_gate: StrategyShadowPromotionGateEvidence = Field(default_factory=StrategyShadowPromotionGateEvidence)


class StrategyPromotionEvidence(_StrictModel):
    automatic_promotion: Literal[False] = False
    eligible_for_manual_review: bool
    observed_independent_session_count: int = Field(ge=0)
    required_independent_session_count: int = Field(ge=1)
    point_in_time_input_integrity_verified: bool
    multiple_testing_method: str
    multiple_testing_ready: bool = False
    pbo_ready: bool
    pbo_status: Literal["not_computed"] = "not_computed"
    deflated_sharpe_status: Literal["not_computed"] = "not_computed"
    blockers: list[str] = Field(default_factory=list, max_length=30)
    conclusion: str


class StrategyEvidenceExecution(_StrictModel):
    execution_id: int | None = Field(default=None, ge=1)
    execution_fingerprint: str | None = None
    market_scan_run_id: int | None = Field(default=None, ge=1)
    rule_version: str | None = None
    production_score_rule_version: str | None = None
    production_score_spec_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    data_as_of: str | None = None
    data_date: str | None = None
    cost_rule_fingerprint: str | None = None
    point_in_time: Literal[True] = True
    evidence_digest_verified: bool


class StrategyEvidenceCenter(_StrictModel):
    evidence_id: int = Field(ge=1)
    schema_version: Literal[1] = 1
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    strategy_fingerprint: str
    strategy_name: str
    mode: Literal["official", "intraday"]
    status: EvidenceCenterStatus
    generated_at: str
    evidence_digest: str
    baseline_generated_at: str | None = None
    baseline_report_digest: str | None = None
    baseline_schema_version: str | None = None
    baseline_projection_schema_version: str | None = None
    research_boundary: StrategyEvidenceResearchBoundary = Field(default_factory=StrategyEvidenceResearchBoundary)
    execution: StrategyEvidenceExecution
    coverage: list[StrategyEvidenceCoverage] = Field(default_factory=list, max_length=10)
    dimensions: list[StrategyDimensionEvidence] = Field(default_factory=list, max_length=6)
    top_n: list[StrategyTopNEvidence] = Field(default_factory=list, max_length=30)
    rank_evidence: list[StrategyRankEvidence] = Field(default_factory=list, max_length=10)
    exposure_audit: dict[str, object] = Field(default_factory=dict)
    shadow_candidates: list[StrategyShadowEvidence] = Field(default_factory=list, max_length=20)
    promotion: StrategyPromotionEvidence
    data_sources: list[str] = Field(default_factory=list, max_length=30)
    freshness_notes: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)


class StrategyEvidenceRefreshRequest(_StrictModel):
    revision: int | None = Field(default=None, ge=1)
    mode: Literal["official", "intraday"] = "official"


__all__ = [
    "EvidenceAvailability",
    "EvidenceCenterStatus",
    "StrategyEvidenceExecutionCompatibility",
    "StrategyDimensionEvidence",
    "StrategyEvidenceCenter",
    "StrategyEvidenceCoverage",
    "StrategyEvidenceExecution",
    "StrategyEvidenceResearchBoundary",
    "StrategyEvidenceRefreshRequest",
    "StrategyPromotionEvidence",
    "StrategyRankEvidence",
    "StrategyShadowConstraintEvidence",
    "StrategyShadowCoverageEvidence",
    "StrategyShadowEvidence",
    "StrategyShadowExposureEvidence",
    "StrategyShadowPromotionGateEvidence",
    "StrategyShadowRankDeltaEvidence",
    "StrategyShadowTopNEvidence",
    "StrategyTopNEvidence",
]
