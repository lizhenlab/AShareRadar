from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import ScenarioPlan


CONFIRMING_VALIDATION_STATUSES = {"条件较好", "等待二次确认"}
TIMEFRAME_WAIT_LEVELS = {"中冲突", "多周期偏弱"}
SCENARIO_MIN_NEUTRAL_PROBABILITY = 10
SCENARIO_POSITIVE_BASE = 32
SCENARIO_RISK_BASE = 28
SCENARIO_FACTOR_WEIGHT = 0.25
SCENARIO_RISK_MULTIPLIER_WEIGHT = 18
SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP = 18
SCENARIO_RISK_PRIORITY_POSITIVE_CAP = 12
SCENARIO_WAIT_CONFIRM_POSITIVE_CAP = 30
DOWNSIDE_MIN_LOSS_PCT = 0.018
DOWNSIDE_NORMAL_BASE_LOSS_PCT = 0.075
DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT = 0.055
DOWNSIDE_HIGH_RISK_MULTIPLIER = 1.18
STRUCTURAL_STOP_MAX_DISTANCE_PCT = 0.12
UPSIDE_TARGET_MIN_CAP_PCT = 0.08
UPSIDE_TARGET_MAX_CAP_PCT = 0.22
UPSIDE_TARGET_ATR_PCT_CAP = 10
UPSIDE_TARGET_VOLATILITY_PCT_CAP = 12


@dataclass(frozen=True)
class RiskRewardRatingContext:
    ratio: float
    factor_score: int
    risk_multiplier: float
    breadth_score: int
    validation_status: str
    timeframe_conflict: str | None


@dataclass(frozen=True)
class RiskRewardRatingRule:
    name: str
    rating: str
    matches: Callable[[RiskRewardRatingContext], bool]


@dataclass(frozen=True)
class DownsideStopContext:
    price: float
    support: float
    ma20: float
    atr14: float
    atr_pct: float
    volatility_pct: float
    risk_multiplier: float


@dataclass(frozen=True)
class DownsideStopAdjustmentRule:
    name: str
    adjustment: float
    matches: Callable[[DownsideStopContext], bool]


@dataclass(frozen=True)
class ScenarioProbabilities:
    positive: int
    neutral: int
    risk: int


@dataclass(frozen=True)
class RiskRewardMetrics:
    price: float
    upside_target: float
    downside_stop: float
    upside_pct: float
    downside_pct: float
    ratio: float
    atr14: float
    atr_pct: float
    volatility_pct: float


@dataclass(frozen=True)
class RiskRewardReportParts:
    metrics: RiskRewardMetrics
    rating: str
    summary: str
    scenarios: list[ScenarioPlan]
    notes: list[str]


@dataclass(frozen=True)
class RiskRewardLevelAvailability:
    price_available: bool
    upside_available: bool
    downside_available: bool
    ratio_available: bool


@dataclass(frozen=True)
class ScenarioPlanContext:
    price: float
    probabilities: ScenarioProbabilities
    validation_status: str
    action: str
    support: float
    resistance: float
    ma20: float
    upside_target: float
    downside_stop: float
