from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from app.models.schemas import (
    AbnormalEventSummary,
    AnalysisResult,
    FundFlowAnalysis,
    OrderPressure,
    RuleDefinition,
    RuleMatch,
    ValuationAnalysis,
)


RULE_VERSION = "rules.v2"
SCORE_VERSION = "score.v2"
STATUS_MATCHED = "命中"
STATUS_CLOSE = "接近"
STATUS_MISSED = "未触发"
LEVEL_RISK = "风险"
LEVEL_POSITIVE = "积极"
LEVEL_WATCH = "观察"
LEVEL_CAUTIOUS = "谨慎"
LEVEL_NEUTRAL = "中性"
QUALITY_GATE_PASS_SCORE = 70
QUALITY_GATE_WEAK_SCORE = 50
QUALITY_GATE_MATCHED_PENALTY = 18
QUALITY_GATE_CLOSE_PENALTY = 10
QUALITY_GATE_MISSED_PENALTY = 6
QUALITY_GATE_RISK_MULTIPLIER = 0.85
QUALITY_GATE_MATCHED_CONFIDENCE_FLOOR = 28
QUALITY_GATE_CLOSE_CONFIDENCE_FLOOR = 24
QUALITY_GATE_MISSED_CONFIDENCE_FLOOR = 20
RULE_CONFIDENCE = {
    "volume_breakout_20d": {STATUS_MATCHED: 78, STATUS_CLOSE: 56, STATUS_MISSED: 35},
    "break_ma20_risk": {STATUS_MATCHED: 82, STATUS_CLOSE: 58, STATUS_MISSED: 38},
    "support_rebound_watch": {STATUS_MATCHED: 72, STATUS_CLOSE: 54, STATUS_MISSED: 32},
    "fund_tech_divergence": {STATUS_MATCHED: 74, STATUS_CLOSE: 55, STATUS_MISSED: 34},
    "high_valuation_chase_risk": {STATUS_MATCHED: 76, STATUS_CLOSE: 55, STATUS_MISSED: 30},
    "abnormal_risk_event": {STATUS_MATCHED: 78, STATUS_CLOSE: 52, STATUS_MISSED: 28},
}
STATUS_SORT_RANK = {STATUS_MATCHED: 0, STATUS_CLOSE: 1, STATUS_MISSED: 2}
LEVEL_SORT_RANK = {
    LEVEL_RISK: 0,
    LEVEL_POSITIVE: 1,
    LEVEL_WATCH: 2,
    LEVEL_CAUTIOUS: 3,
    LEVEL_NEUTRAL: 4,
}
RuleParameter: TypeAlias = float | int | str
RULE_PARAMETERS_BY_ID: dict[str, dict[str, RuleParameter]] = {
    "volume_breakout_20d": {"near_breakout_pct": 0.985, "volume_ratio": 1.35, "window": 20},
    "break_ma20_risk": {"trend_score": 50, "near_ma20_pct": 1.015},
    "support_rebound_watch": {"near_support_pct": 1.03, "fund_score": 58},
    "fund_tech_divergence": {"trend_weak": 48, "trend_strong": 65, "fund_strong": 62, "fund_weak": 48, "gap": 18},
    "high_valuation_chase_risk": {"trend_hit": 68, "trend_close": 62, "valuation_hit": 45, "valuation_close": 52},
    "abnormal_risk_event": {"risk_event_min": 1},
}
RULE_DEFINITION_FIELDS = {
    "volume_breakout_20d": (
        "放量突破20日高点",
        "趋势",
        "价格接近或突破近20日高点，同时量能明显高于近5日均量。",
        "这是右侧确认信号，重点看突破后是否站稳，而不是盘中一冲就追。",
    ),
    "break_ma20_risk": (
        "跌破20日线风险",
        "风控",
        "现价低于20日均线且趋势评分偏弱。",
        "20日线是波段风控线，跌破后先降低乐观预期。",
    ),
    "support_rebound_watch": (
        "支撑位止跌观察",
        "买点观察",
        "价格接近支撑位，下影或量价热度（衍生）出现承接迹象。",
        "这是观察信号，不是越跌越买；必须有止跌证据。",
    ),
    "fund_tech_divergence": (
        "量价技术背离",
        "量价衍生",
        "趋势与量价热度评分（衍生）出现明显分歧。",
        "分歧阶段不要只看一个指标，等待价格或量价热度（衍生）给出一致方向。",
    ),
    "high_valuation_chase_risk": (
        "高估值追高风险",
        "估值",
        "趋势强但估值压力偏高，容易出现波动放大。",
        "强势股也需要风控线，估值越贵越不能忽略失效条件。",
    ),
    "abnormal_risk_event": (
        "风险异动降级",
        "事件",
        "出现放量下跌、跌停附近、长上影等风险异动。",
        "风险异动先解释原因，再决定是否继续观察。",
    ),
}
@dataclass(frozen=True)
class VolumeBreakoutState:
    near_breakout: bool
    enough_volume: bool


@dataclass(frozen=True)
class BreakMa20State:
    broken: bool
    close: bool


@dataclass(frozen=True)
class FundTechDivergenceState:
    positive_divergence: bool
    negative_divergence: bool
    gap_reached: bool


@dataclass(frozen=True)
class RuleMatchContext:
    analysis: AnalysisResult
    fund_flow: FundFlowAnalysis
    order_pressure: OrderPressure
    valuation: ValuationAnalysis
    abnormal_events: AbnormalEventSummary
    latest_high_20: float
    volume_ratio: float | None


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    category: str
    description: str
    beginner_hint: str
    evaluate: Callable[[RuleMatchContext], RuleMatch]

    def definition(self) -> RuleDefinition:
        return RuleDefinition(
            id=self.id,
            name=self.name,
            category=self.category,
            description=self.description,
            beginner_hint=self.beginner_hint,
            version=RULE_VERSION,
            parameters=dict(RULE_PARAMETERS_BY_ID[self.id]),
        )


@dataclass(frozen=True)
class QualityGateContext:
    match: RuleMatch
    score: int
    level: str


@dataclass(frozen=True)
class QualityGateDecision:
    status: str
    level: str
    confidence: int


@dataclass(frozen=True)
class HighValuationChaseState:
    hit: bool
    close: bool


@dataclass(frozen=True)
class SupportReboundState:
    has_support: bool
    near_support: bool
    has_rebound: bool
    has_risk_event: bool


def rule_spec(rule_id: str, evaluate: Callable[[RuleMatchContext], RuleMatch]) -> RuleSpec:
    name, category, description, beginner_hint = RULE_DEFINITION_FIELDS[rule_id]
    return RuleSpec(rule_id, name, category, description, beginner_hint, evaluate)


def _rule_match_fields(rule_id: str) -> dict[str, str]:
    name, category, _description, _beginner_hint = RULE_DEFINITION_FIELDS[rule_id]
    return {
        "rule_id": rule_id,
        "name": name,
        "category": category,
        "rule_version": RULE_VERSION,
        "score_version": SCORE_VERSION,
    }
