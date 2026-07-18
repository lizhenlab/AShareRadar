from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import AnalysisResult, ChipAnalysis, FeatureSnapshot, StandardFactor, StockInsightBundle
from app.services.indicators import pct_change
from app.services.research_factor_calibration import _calibrate_factor, _calibration_buckets, _factor_percentile
from app.services.research_factor_specs import FactorSpec
from app.services.research_factor_weights import _adjusted_factor_weight
from app.services.scoring import clamp_score as _clamp, score_level as _score_level


@dataclass(frozen=True)
class ChipFallbackContext:
    price: float
    support: float
    resistance: float


@dataclass(frozen=True)
class ChipFallbackRule:
    name: str
    score: int
    matches: Callable[[ChipFallbackContext], bool]


@dataclass(frozen=True)
class ChipDistanceRule:
    name: str
    adjustment: int
    matches: Callable[[float], bool]


@dataclass(frozen=True)
class VolumeConfirmationContext:
    ratio: float
    change_pct: float


@dataclass(frozen=True)
class VolumeConfirmationRule:
    name: str
    adjustment: Callable[[VolumeConfirmationContext], int]
    matches: Callable[[VolumeConfirmationContext], bool]


@dataclass(frozen=True)
class RiskPressureContext:
    risk_level: str
    data_quality_score: int
    abnormal_level: str
    order_pressure: str
    price: float
    ma20: float


@dataclass(frozen=True)
class RiskPressureRule:
    name: str
    adjustment: Callable[[RiskPressureContext], int]
    matches: Callable[[RiskPressureContext], bool]


VOLUME_CONFIRMATION_BASE_SCORE = 52
RISK_PRESSURE_BASE_SCORE = 72


def _build_factor(
    spec: FactorSpec,
    analysis: AnalysisResult,
    score: int,
    value: str,
    evidence: list[str],
    missing_data: list[str],
    weight_adjustments: dict[str, float] | None = None,
    *,
    data_nature: str | None = None,
    methodology: str | None = None,
) -> StandardFactor:
    clean_score = _clamp(score)
    return StandardFactor(
        id=spec.id,
        name=spec.name,
        category=spec.category,
        value=value,
        score=clean_score,
        level=_score_level(clean_score),
        direction=_factor_direction(clean_score),
        percentile=_factor_percentile(analysis.klines, spec.evaluator, clean_score),
        weight=_adjusted_factor_weight(spec.id, spec.weight, weight_adjustments or {}),
        evidence=evidence[:4],
        missing_data=_dedupe(missing_data)[:6],
        calibration=_calibrate_factor(analysis.klines, spec, clean_score),
        calibration_buckets=_calibration_buckets(analysis.klines, spec, clean_score),
        data_nature=data_nature,
        methodology=methodology,
    )


def _weighted_factor_score(factors: list[StandardFactor]) -> int:
    total_weight = sum(item.weight for item in factors) or 1
    return _clamp(round(sum(item.score * item.weight for item in factors) / total_weight))


def _factor_participates_in_historical_aggregate(factor: StandardFactor) -> bool:
    calibration = factor.calibration
    return bool(calibration and getattr(calibration, "participates_in_historical_aggregate", True))


def _historical_aggregate_factors(factors: list[StandardFactor]) -> list[StandardFactor]:
    return [item for item in factors if _factor_participates_in_historical_aggregate(item)]


def _factor_calibration_quality(factors: list[StandardFactor]) -> int:
    scored = [
        item.calibration
        for item in _historical_aggregate_factors(factors)
        if item.calibration and item.calibration.sample_count > 0
    ]
    if not scored:
        return 35
    total_weight = sum(min(1.6, max(0.6, item.sample_count / 12)) for item in scored)
    weighted = sum(item.stability_score * min(1.6, max(0.6, item.sample_count / 12)) for item in scored)
    coverage_bonus = min(10, len(scored) * 2)
    return _clamp(round(weighted / total_weight + coverage_bonus))


def _volume_confirmation_score(analysis: AnalysisResult, feature: FeatureSnapshot) -> int:
    context = VolumeConfirmationContext(ratio=feature.volume_ratio, change_pct=analysis.quote.change_pct)
    adjustment = _volume_confirmation_adjustment(context)
    return _clamp(VOLUME_CONFIRMATION_BASE_SCORE + adjustment)


def _volume_confirmation_adjustment(context: VolumeConfirmationContext) -> int:
    for rule in VOLUME_CONFIRMATION_RULES:
        if rule.matches(context):
            return rule.adjustment(context)
    return 0


def _positive_volume_adjustment(context: VolumeConfirmationContext) -> int:
    return 18 + _volume_expansion_bonus(context.ratio)


def _negative_volume_adjustment(context: VolumeConfirmationContext) -> int:
    return -18 - _volume_expansion_bonus(context.ratio)


def _volume_expansion_bonus(ratio: float) -> int:
    return round(min(10, (ratio - 1.2) * 8))


VOLUME_CONFIRMATION_RULES = (
    VolumeConfirmationRule("positive_volume_expansion", _positive_volume_adjustment, lambda context: context.change_pct > 0 and context.ratio >= 1.2),
    VolumeConfirmationRule("negative_volume_expansion", _negative_volume_adjustment, lambda context: context.change_pct < 0 and context.ratio >= 1.2),
    VolumeConfirmationRule("low_volume_large_move", lambda context: -8, lambda context: context.ratio < 0.7 and abs(context.change_pct) >= 2),
    VolumeConfirmationRule("normal_volume", lambda context: 4, lambda context: 0.85 <= context.ratio <= 1.25),
)


def _risk_pressure_score(analysis: AnalysisResult, insights: StockInsightBundle, feature: FeatureSnapshot) -> int:
    context = RiskPressureContext(
        risk_level=analysis.risk_level,
        data_quality_score=feature.data_quality_score,
        abnormal_level=insights.abnormal_events.level,
        order_pressure=feature.order_pressure,
        price=feature.price,
        ma20=feature.ma20,
    )
    score = RISK_PRESSURE_BASE_SCORE + sum(_risk_pressure_adjustments(context))
    return _clamp(score)


def _risk_pressure_adjustments(context: RiskPressureContext) -> list[int]:
    adjustments = [rule.adjustment(context) for rule in RISK_PRESSURE_RULES if rule.matches(context)]
    adjustments.append(_risk_pressure_quality_adjustment(context))
    return adjustments


def _risk_pressure_quality_adjustment(context: RiskPressureContext) -> int:
    return round((context.data_quality_score - 80) * 0.22)


def _risk_level_adjustment(context: RiskPressureContext) -> int:
    return {"高风险": -32, "中等风险": -16, "低风险": 6}.get(context.risk_level, 0)


def _chip_position_score_current(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> int:
    if not _has_chip_model(chip):
        return _chip_fallback_score(feature)
    assert chip is not None
    distance = pct_change(feature.price, chip.center_price)
    score = 58 + _chip_distance_adjustment(distance) + _chip_concentration_adjustment(chip.concentration)
    return _clamp(score)


RISK_PRESSURE_RULES = (
    RiskPressureRule("risk_level", _risk_level_adjustment, lambda context: context.risk_level in {"高风险", "中等风险", "低风险"}),
    RiskPressureRule("abnormal_risk", lambda context: -14, lambda context: context.abnormal_level == "风险"),
    RiskPressureRule("sell_pressure", lambda context: -8, lambda context: "卖压" in context.order_pressure),
    RiskPressureRule("below_ma20", lambda context: -8, lambda context: context.price < context.ma20),
)


def _has_chip_model(chip: ChipAnalysis | None) -> bool:
    return bool(chip and chip.center_price > 0)


def _chip_fallback_score(feature: FeatureSnapshot) -> int:
    context = ChipFallbackContext(price=feature.price, support=feature.support, resistance=feature.resistance)
    for rule in CHIP_FALLBACK_RULES:
        if rule.matches(context):
            return rule.score
    return 52


def _near_resistance_without_chip(context: ChipFallbackContext) -> bool:
    return bool(context.resistance and context.price >= context.resistance * 0.99)


def _near_support_without_chip(context: ChipFallbackContext) -> bool:
    return bool(context.support and context.price <= context.support * 1.03)


CHIP_FALLBACK_RULES = (
    ChipFallbackRule("near_resistance", 54, _near_resistance_without_chip),
    ChipFallbackRule("near_support", 48, _near_support_without_chip),
)


def _chip_distance_adjustment(distance: float) -> int:
    for rule in CHIP_DISTANCE_RULES:
        if rule.matches(distance):
            return rule.adjustment
    return 0


def _chip_concentration_adjustment(concentration: int) -> int:
    return round((concentration - 50) * 0.22)


CHIP_DISTANCE_RULES = (
    ChipDistanceRule("near_cost_center", 16, lambda distance: -3 <= distance <= 8),
    ChipDistanceRule("moderately_above_center", 4, lambda distance: 8 < distance <= 16),
    ChipDistanceRule("overheated_above_center", -14, lambda distance: distance > 16),
    ChipDistanceRule("deep_below_center", -12, lambda distance: distance < -8),
)


def _chip_position_value(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> str:
    if not chip:
        return f"现价 {feature.price:.2f} / 支撑 {feature.support:.2f} / 压力 {feature.resistance:.2f}"
    return f"现价较成本中枢 {pct_change(feature.price, chip.center_price):.2f}% / 集中度 {chip.concentration}"


def _chip_position_evidence(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> list[str]:
    if not chip:
        return [f"支撑位 {feature.support:.2f}，压力位 {feature.resistance:.2f}。"]
    evidence = [chip.summary]
    if chip.support_bands:
        band = chip.support_bands[0]
        evidence.append(f"最近支撑筹码区 {band.low:.2f}-{band.high:.2f}，占比 {band.share:.1f}%。")
    if chip.pressure_bands:
        band = chip.pressure_bands[0]
        evidence.append(f"最近压力筹码区 {band.low:.2f}-{band.high:.2f}，占比 {band.share:.1f}%。")
    return evidence


def _factor_direction(score: int) -> str:
    if score >= 58:
        return "正向"
    if score <= 45:
        return "负向"
    return "中性"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


__all__ = [
    "_build_factor",
    "_chip_position_evidence",
    "_chip_position_score_current",
    "_chip_position_value",
    "_dedupe",
    "_factor_calibration_quality",
    "_factor_direction",
    "_factor_participates_in_historical_aggregate",
    "_historical_aggregate_factors",
    "_risk_pressure_score",
    "RISK_PRESSURE_RULES",
    "VOLUME_CONFIRMATION_RULES",
    "_volume_confirmation_score",
    "_weighted_factor_score",
]
