"""Compact response contracts for the strategy evidence center."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EvidenceCenterStatus = Literal["insufficient_data", "blocked", "eligible_for_manual_review"]


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


class StrategyShadowEvidence(_StrictModel):
    candidate_id: str
    status: str
    spec_hash: str | None = None
    point_in_time_integrity_verified: bool
    independent_session_count: int = Field(ge=0)


class StrategyPromotionEvidence(_StrictModel):
    automatic_promotion: Literal[False] = False
    eligible_for_manual_review: bool
    observed_independent_session_count: int = Field(ge=0)
    required_independent_session_count: int = Field(ge=1)
    point_in_time_input_integrity_verified: bool
    multiple_testing_method: str
    pbo_ready: bool
    blockers: list[str] = Field(default_factory=list, max_length=30)
    conclusion: str


class StrategyEvidenceExecution(_StrictModel):
    execution_id: int | None = Field(default=None, ge=1)
    execution_fingerprint: str | None = None
    market_scan_run_id: int | None = Field(default=None, ge=1)
    rule_version: str | None = None
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
    "EvidenceCenterStatus",
    "StrategyDimensionEvidence",
    "StrategyEvidenceCenter",
    "StrategyEvidenceCoverage",
    "StrategyEvidenceExecution",
    "StrategyEvidenceRefreshRequest",
    "StrategyPromotionEvidence",
    "StrategyRankEvidence",
    "StrategyShadowEvidence",
    "StrategyTopNEvidence",
]
