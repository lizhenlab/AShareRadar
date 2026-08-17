"""Research, diagnosis, question-answering, replay, and intraday report models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.models.analysis import PeerSampleInfo
from app.models.market import MinuteKline, StockConceptItem


def _composite_reliability_level(value: int) -> str:
    if value >= 75:
        return "较高"
    if value >= 55:
        return "中等"
    if value >= 35:
        return "较低"
    return "不足"


class AlphaEvidencePoint(BaseModel):
    source: str
    title: str
    impact: int
    level: str
    reason: str


class AlphaEvidenceReport(BaseModel):
    symbol: str
    updated_at: str
    confidence: int = Field(description="兼容字段：0-100 的 Alpha 证据充分度评分，不代表统计置信度或概率")
    confidence_semantics: Literal["non_statistical_evidence_sufficiency"] = "non_statistical_evidence_sufficiency"
    confidence_note: str = "该值由规则证据、数据质量和风险约束综合形成，是启发式评分，不是统计置信度或命中概率。"
    verdict: str
    summary: str
    positives: list[AlphaEvidencePoint] = Field(default_factory=list)
    negatives: list[AlphaEvidencePoint] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)


class FactorCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sample_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=100)
    avg_forward_5d_return: float
    avg_forward_10d_return: float
    max_adverse_return: float
    stability_score: int = Field(default=0, ge=0, le=100)
    expected_level: str = "观察"
    confidence_level: str
    participates_in_historical_aggregate: bool = True
    availability: Literal["available", "insufficient_history", "no_similar_samples", "execution_evidence_unavailable"] = "available"
    unavailable_reason: str | None = None
    execution_contract_version: str | None = None
    note: str

    @model_validator(mode="after")
    def validate_availability(self) -> FactorCalibration:
        if self.availability != "available":
            if self.participates_in_historical_aggregate:
                raise ValueError("unavailable calibration cannot enter historical aggregate")
            if not str(self.unavailable_reason or "").strip():
                raise ValueError("unavailable calibration requires a reason")
        if self.participates_in_historical_aggregate and self.sample_count <= 0:
            raise ValueError("historical aggregate calibration requires positive samples")
        return self


class CalibrationBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str
    sample_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=100)
    avg_forward_5d_return: float
    avg_forward_10d_return: float
    note: str


class StandardFactor(BaseModel):
    id: str
    name: str
    category: str
    value: str
    score: int
    level: str
    direction: str
    percentile: float | None = None
    weight: float
    participates_in_current_score: bool = True
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    calibration: FactorCalibration | None = None
    calibration_buckets: list[CalibrationBucket] = Field(default_factory=list)
    data_nature: Literal["derived", "estimated", "observed", "unavailable"] | None = None
    methodology: str | None = None

    @model_validator(mode="after")
    def validate_current_score_availability(self) -> StandardFactor:
        if self.data_nature == "unavailable" and self.participates_in_current_score:
            raise ValueError("unavailable factor cannot participate in current score")
        return self


class FactorLabReport(BaseModel):
    symbol: str
    updated_at: str
    total_score: int
    calibrated_confidence: int = Field(description="兼容字段：非统计的综合证据充分度，不代表置信区间或命中概率")
    evidence_sufficiency: int | None = Field(default=None, description="综合因子证据充分度，非统计置信度")
    composite_reliability_level: str | None = Field(default=None, description="综合可信等级，非统计口径")
    confidence_semantics: Literal["non_statistical_evidence_sufficiency"] = "non_statistical_evidence_sufficiency"
    evidence_sufficiency_note: str = "综合值由因子、数据质量、样本稳定性和正负证据共同形成，不是统计置信度或概率。"
    calibration_sample_count: int = 0
    positive_factor_count: int = 0
    negative_factor_count: int = 0
    profile_label: str = "常规个股"
    weight_policy: list[str] = Field(default_factory=list)
    factors: list[StandardFactor]
    top_positive: list[str] = Field(default_factory=list)
    top_negative: list[str] = Field(default_factory=list)
    summary: str
    notes: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if self.evidence_sufficiency is None:
            self.evidence_sufficiency = self.calibrated_confidence
        if self.composite_reliability_level is None:
            self.composite_reliability_level = _composite_reliability_level(self.evidence_sufficiency)


class MarketRegimeReport(BaseModel):
    symbol: str
    updated_at: str
    market_label: str
    breadth_label: str = "市场宽度待确认"
    breadth_score: int = 50
    industry_label: str
    stock_state: str
    risk_multiplier: float
    confidence_adjustment: int = Field(description="兼容字段：对启发式证据充分度评分的加减分，不是概率修正")
    confidence_adjustment_semantics: Literal["non_statistical_evidence_sufficiency_adjustment"] = (
        "non_statistical_evidence_sufficiency_adjustment"
    )
    suggestions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class SignalValidationItem(BaseModel):
    name: str
    category: str
    status: str
    confidence: int = Field(description="兼容字段：0-100 的规则验证强度评分，不代表统计置信度或命中概率")
    confidence_semantics: Literal["non_statistical_validation_strength"] = "non_statistical_validation_strength"
    confidence_note: str = "该值由触发、确认、失效条件及数据质量综合形成，是启发式验证强度，不是统计概率。"
    trigger_condition: str
    confirmation_condition: str
    invalidation_condition: str
    historical_reference: str
    action_hint: str


class SignalValidationReport(BaseModel):
    symbol: str
    updated_at: str
    overall_status: str
    summary: str
    items: list[SignalValidationItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ScenarioPlan(BaseModel):
    name: str
    probability: int = Field(description="兼容字段：启发式规则情景权重，不代表统计概率")
    rule_weight: int | None = Field(default=None, description="归一化到 100 的启发式规则情景权重")
    weight_basis: Literal["heuristic_rule_weight"] = "heuristic_rule_weight"
    trigger: str
    expected_move: str
    response: str
    invalidation: str

    def model_post_init(self, __context: object) -> None:
        if self.rule_weight is None:
            self.rule_weight = self.probability


class RiskRewardReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    updated_at: str
    current_price: float
    upside_target: float
    downside_stop: float
    upside_pct: float
    downside_pct: float
    reward_risk_ratio: float
    atr14: float = 0
    atr_pct: float = 0
    volatility_pct: float = 0
    level_availability_contract_version: Literal["risk-reward-level-availability.v1"] = "risk-reward-level-availability.v1"
    upside_available: bool = False
    downside_available: bool = False
    ratio_available: bool = False
    upside_target_basis: Literal["resistance", "atr", "resistance_and_atr", "unavailable"] = "unavailable"
    downside_stop_basis: Literal["structure", "atr", "structure_and_atr", "unavailable"] = "unavailable"
    availability_reason: str | None = None
    rating: str
    summary: str
    scenario_weight_basis: Literal["heuristic_rule_weight"] = "heuristic_rule_weight"
    scenario_weight_note: str = "规则情景权重由启发式规则归一化，仅用于情景比较，不是统计概率。"
    scenarios: list[ScenarioPlan] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_level_availability(self) -> RiskRewardReport:
        _validate_risk_reward_upside(self)
        _validate_risk_reward_downside(self)
        _validate_risk_reward_ratio(self)
        return self


def _validate_risk_reward_upside(report: RiskRewardReport) -> None:
    available = report.upside_target_basis != "unavailable" and report.upside_target > report.current_price
    if report.upside_available and (not available or report.upside_pct <= 0):
        raise ValueError("available upside requires an evidenced positive target")
    if not report.upside_available and (report.upside_target_basis != "unavailable" or report.upside_target != 0 or report.upside_pct != 0):
        raise ValueError("unavailable upside must not expose a synthetic target")


def _validate_risk_reward_downside(report: RiskRewardReport) -> None:
    available = (
        report.downside_stop_basis != "unavailable"
        and 0 < report.downside_stop < report.current_price
    )
    if report.downside_available and (not available or report.downside_pct <= 0):
        raise ValueError("available downside requires an evidenced positive distance")
    if not report.downside_available and (report.downside_stop_basis != "unavailable" or report.downside_stop != 0 or report.downside_pct != 0):
        raise ValueError("unavailable downside must not expose a synthetic stop")


def _validate_risk_reward_ratio(report: RiskRewardReport) -> None:
    both_available = report.upside_available and report.downside_available
    if report.ratio_available and not both_available:
        raise ValueError("available reward/risk ratio requires both evidenced sides")
    ratio_valid = report.reward_risk_ratio > 0 if report.ratio_available else report.reward_risk_ratio == 0
    if not ratio_valid:
        raise ValueError("reward/risk ratio conflicts with availability")
    if not both_available and not str(report.availability_reason or "").strip():
        raise ValueError("unavailable risk/reward levels require a reason")


class TimeframeTrend(BaseModel):
    name: str
    window_days: int
    score: int
    label: str
    return_pct: float
    max_drawdown_pct: float
    above_ma: bool
    ma_value: float
    evidence: list[str] = Field(default_factory=list)


class TimeframeAlignmentReport(BaseModel):
    symbol: str
    updated_at: str
    alignment_score: int
    alignment_label: str
    conflict_level: str
    summary: str
    timeframes: list[TimeframeTrend] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class StockDiagnosis(BaseModel):
    symbol: str
    updated_at: str
    headline: str
    beginner_summary: str
    professional_summary: str
    confirmation_signals: list[str] = Field(default_factory=list)
    hard_risks: list[str] = Field(default_factory=list)
    watch_focus: list[str] = Field(default_factory=list)
    action: str
    confidence: int = Field(description="兼容字段：0-100 的诊断证据充分度评分，不代表统计置信度或命中概率")
    confidence_semantics: Literal["non_statistical_evidence_sufficiency"] = "non_statistical_evidence_sufficiency"
    confidence_note: str = "该值衡量当前诊断依据的充分程度，是启发式评分，不是统计置信度或命中概率。"


class EvidenceChainReport(BaseModel):
    symbol: str
    updated_at: str
    verdict: str
    summary: str
    support: list[str] = Field(default_factory=list)
    opposition: list[str] = Field(default_factory=list)
    confirmations: list[str] = Field(default_factory=list)
    invalidations: list[str] = Field(default_factory=list)


class StockQaItem(BaseModel):
    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)


class StockQaReport(BaseModel):
    symbol: str
    updated_at: str
    summary: str
    items: list[StockQaItem] = Field(default_factory=list)


class StockQuestionInput(BaseModel):
    symbol: str = Field(default="600519", min_length=1, max_length=20)
    question: str = Field(min_length=2, max_length=120)


class StockQuestionAnswer(BaseModel):
    symbol: str
    updated_at: str
    question: str
    topic: str
    conclusion: str
    answer: str
    confidence: int = Field(description="兼容字段：0-100 的回答可靠度评分，不代表统计正确率或概率")
    confidence_semantics: Literal["non_statistical_answer_reliability"] = "non_statistical_answer_reliability"
    confidence_note: str = "该值由规则问诊、诊断证据和数据质量综合形成，是启发式回答可靠度，不是统计正确率或概率。"
    answer_source: str = "规则问诊"
    llm_used: bool = False
    llm_status: str | None = None
    evidence: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    invalidations: list[str] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)


class EventDigestReport(BaseModel):
    symbol: str
    updated_at: str
    impact_label: str
    summary: str
    positive_events: list[str] = Field(default_factory=list)
    negative_events: list[str] = Field(default_factory=list)
    watch_events: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class PeerComparisonReport(BaseModel):
    symbol: str
    updated_at: str
    industry: str = "行业待确认"
    sample_count: int = 0
    valuation_position: str = "同行估值待确认"
    strength_position: str = "同行强弱待确认"
    summary: str
    metrics: list[str] = Field(default_factory=list)
    leaders: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    sample_status: PeerSampleInfo = Field(default_factory=PeerSampleInfo)
    warnings: list[str] = Field(default_factory=list)


class TStrategyAssistantReport(BaseModel):
    symbol: str
    updated_at: str
    style: str
    suitability: str
    summary: str
    low_zone: str
    high_zone: str
    execution_steps: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class RiskRadarItem(BaseModel):
    name: str
    level: str
    score: int
    score_available: bool = True
    data_nature: Literal["derived", "unavailable"] = "derived"
    reason: str
    action: str

    @model_validator(mode="after")
    def validate_score_availability(self) -> RiskRadarItem:
        if self.score_available:
            if self.data_nature == "unavailable":
                raise ValueError("available risk score requires derived evidence")
        elif self.data_nature != "unavailable" or self.score != 0 or self.level != "不可用":
            raise ValueError("unavailable risk score must use the unavailable representation")
        return self


class RiskRadarReport(BaseModel):
    symbol: str
    updated_at: str
    overall_level: str
    summary: str
    items: list[RiskRadarItem] = Field(default_factory=list)
    top_risks: list[str] = Field(default_factory=list)


class ChipBand(BaseModel):
    label: str
    low: float
    high: float
    share: float
    note: str


class ChipAnalysis(BaseModel):
    symbol: str
    updated_at: str
    distribution_available: bool = False
    data_nature: Literal["derived", "unavailable"] = "unavailable"
    valid_session_count: int = Field(default=0, ge=0)
    center_price: float = Field(ge=0)
    concentration: int = Field(ge=0, le=100)
    distribution_label: str
    summary: str
    support_bands: list[ChipBand] = Field(default_factory=list)
    pressure_bands: list[ChipBand] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_distribution_availability(self) -> ChipAnalysis:
        if self.distribution_available:
            if self.data_nature != "derived" or self.valid_session_count <= 0 or self.center_price <= 0:
                raise ValueError("available chip distribution requires derived positive evidence")
        elif self.data_nature != "unavailable" or self.support_bands or self.pressure_bands:
            raise ValueError("unavailable chip distribution cannot expose derived bands")
        return self


class LeadershipReport(BaseModel):
    symbol: str
    updated_at: str
    score: int
    level: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class ThemeContextReport(BaseModel):
    symbol: str
    updated_at: str
    industry: str = "行业待确认"
    industry_change_pct: float | None = None
    concepts: list[StockConceptItem] = Field(default_factory=list)
    score: int = 0
    level: str = "主题待确认"
    style: str = "背景不足"
    relative_strength: str = "强弱待确认"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class ReplayPatternStat(BaseModel):
    pattern: str
    sample_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    win_rate: FiniteFloat | None = Field(default=None, ge=0, le=100)
    avg_forward_5d_return: FiniteFloat | None = None
    excess_vs_baseline_pct: FiniteFloat | None = None
    note: str

    @model_validator(mode="after")
    def validate_evaluated_metrics(self) -> ReplayPatternStat:
        if self.evaluated_count > self.sample_count:
            raise ValueError("evaluated replay count cannot exceed observed samples")
        metrics = (
            self.win_rate,
            self.avg_forward_5d_return,
            self.excess_vs_baseline_pct,
        )
        if self.evaluated_count == 0 and any(value is not None for value in metrics):
            raise ValueError("unevaluated replay pattern cannot expose performance metrics")
        if self.evaluated_count > 0 and any(value is None for value in metrics):
            raise ValueError("evaluated replay pattern requires performance metrics")
        return self


class ReplayCase(BaseModel):
    date: str
    pattern: str
    entry_price: float
    forward_3d_return: float | None = None
    forward_5d_return: float | None = None
    forward_10d_return: float | None = None
    outcome: str
    note: str
    trend_regime: str = "未分类"


class ReplayRegimeStat(BaseModel):
    regime: str
    sample_count: int
    evaluated_count: int
    win_rate: float | None = None
    avg_forward_5d_return: float | None = None


class StockReplayAnalysis(BaseModel):
    symbol: str
    updated_at: str
    availability: Literal[
        "available",
        "insufficient_history",
        "execution_evidence_unavailable",
    ] = "available"
    unavailable_reason: str | None = None
    window_days: int
    sample_count: int
    success_rate: FiniteFloat | None = Field(default=None, ge=0, le=100)
    baseline_win_rate: FiniteFloat | None = Field(default=None, ge=0, le=100)
    baseline_avg_forward_5d_return: FiniteFloat | None = None
    excess_vs_baseline_pct: FiniteFloat | None = None
    modelled_round_trip_friction_pct: FiniteFloat = Field(ge=0, le=100)
    summary: str
    pattern_stats: list[ReplayPatternStat] = Field(default_factory=list)
    regime_stats: list[ReplayRegimeStat] = Field(default_factory=list)
    cases: list[ReplayCase] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_replay_availability(self) -> StockReplayAnalysis:
        metrics = (
            self.success_rate,
            self.baseline_win_rate,
            self.baseline_avg_forward_5d_return,
            self.excess_vs_baseline_pct,
        )
        if self.availability != "available" and any(value is not None for value in metrics):
            raise ValueError("unavailable replay cannot expose performance metrics")
        if self.availability == "available":
            if self.unavailable_reason is not None:
                raise ValueError("available replay cannot expose an unavailable reason")
            if any(value is None for value in metrics[:3]):
                raise ValueError("available replay requires mature and baseline metrics")
            return self
        if self.availability != "available" and not str(self.unavailable_reason or "").strip():
            raise ValueError("unavailable replay requires a reason")
        return self


class MinuteSupportResistance(BaseModel):
    label: str
    price: float
    strength: int
    reason: str


class MinuteTPlan(BaseModel):
    low_zone: str
    high_zone: str
    suitability: str
    style: str
    confidence: int
    summary: str
    execution_steps: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class MinuteAnalysisReport(BaseModel):
    symbol: str
    updated_at: str
    interval: str
    source: str
    sample_count: int
    klines: list[MinuteKline] = Field(
        default_factory=list,
        description=(
            "分析实际使用的有效分钟K线，按 timestamp 升序且去重。availability=unavailable 时仅供数据审计："
            "provider/空数据返回空，样本不足可返回过滤后行；UI 不得据此形成执行区间。"
        ),
    )
    availability: Literal["ok", "degraded", "unavailable"] = "unavailable"
    availability_reason: str = "未提供分钟分析可用性状态，按不可用处理。"
    reason_code: str = "legacy_status_missing"
    latest_price: float | None = None
    intraday_change_pct: float = 0
    intraday_range_pct: float = 0
    volume_pulse: str = "待确认"
    trend_label: str = "待确认"
    momentum_label: str = "待确认"
    summary: str
    supports: list[MinuteSupportResistance] = Field(default_factory=list)
    resistances: list[MinuteSupportResistance] = Field(default_factory=list)
    t_plan: MinuteTPlan
    warnings: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if self.availability != "unavailable":
            return
        self.supports = []
        self.resistances = []
        self.t_plan = self.t_plan.model_copy(
            update={
                "low_zone": "不可用",
                "high_zone": "不可用",
                "suitability": "暂停做T判断",
                "style": "数据不可用",
                "confidence": 0,
                "summary": self.availability_reason,
                "execution_steps": ["等待有效分钟K线恢复并重新分析后，再形成盘中参考区间。"],
                "stop_conditions": ["分钟分析不可用期间，不按盘中区间执行做T。"],
            }
        )
