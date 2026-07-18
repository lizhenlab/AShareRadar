from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import FactorLabReport, FeatureSnapshot, StandardFactor
from app.services.research_factor_scoring import (
    _factor_calibration_quality,
    _factor_participates_in_historical_aggregate,
    _historical_aggregate_factors,
    _weighted_factor_score,
)
from app.services.research_factor_text import _factor_lab_summary, _factor_score_impact
from app.services.scoring import clamp_score as _clamp


@dataclass(frozen=True)
class FactorLabMetrics:
    total_score: int
    calibrated_confidence: int
    calibration_sample_count: int
    positives: list[str]
    negatives: list[str]
    scoring_factor_count: int = 0
    calibration_factor_count: int = 0
    uncalibrated_factor_names: tuple[str, ...] = ()


def build_factor_lab_metrics(factors: list[StandardFactor], feature: FeatureSnapshot) -> FactorLabMetrics:
    total_score = _weighted_factor_score(factors)
    historical_factors = _historical_aggregate_factors(factors)
    evidence_factor_score = _weighted_factor_score(historical_factors)
    calibration_sample_count = _effective_calibration_sample_count(historical_factors)
    calibration_factor_count = len(historical_factors)
    uncalibrated_factor_names = tuple(
        item.name for item in factors if not _factor_participates_in_historical_calibration(item)
    )
    support_count = factor_support_count(historical_factors)
    risk_count = factor_risk_count(historical_factors)
    calibration_quality = _factor_calibration_quality(historical_factors)
    calibrated_confidence = _clamp(
        round(
            evidence_factor_score * 0.45
            + feature.signal_confidence * 0.2
            + feature.data_quality_score * 0.22
            + calibration_quality * 0.13
            + support_count * 3
            - risk_count * 4
        )
    )
    return FactorLabMetrics(
        total_score=total_score,
        calibrated_confidence=calibrated_confidence,
        calibration_sample_count=calibration_sample_count,
        positives=top_positive_factors(historical_factors),
        negatives=top_negative_factors(historical_factors),
        scoring_factor_count=len(factors),
        calibration_factor_count=calibration_factor_count,
        uncalibrated_factor_names=uncalibrated_factor_names,
    )


def _factor_participates_in_historical_calibration(factor: StandardFactor) -> bool:
    return _factor_participates_in_historical_aggregate(factor)


def _effective_calibration_sample_count(factors: list[StandardFactor]) -> int:
    """Keep aggregate support conservative when factor samples reuse trading dates."""
    sample_counts = [
        max(0, item.calibration.sample_count)
        for item in _historical_aggregate_factors(factors)
        if item.calibration is not None
    ]
    return min(sample_counts, default=0)


def factor_support_count(factors: list[StandardFactor]) -> int:
    return sum(
        1
        for item in _historical_aggregate_factors(factors)
        if item.score >= 60
        and item.calibration
        and item.calibration.sample_count >= 5
        and item.calibration.expected_level in {"偏正", "较强"}
    )


def factor_risk_count(factors: list[StandardFactor]) -> int:
    return sum(
        1
        for item in _historical_aggregate_factors(factors)
        if (
            item.score <= 45
            or (item.calibration and item.calibration.sample_count >= 5 and item.calibration.expected_level in {"偏弱", "风险"})
        )
    )


def top_positive_factors(factors: list[StandardFactor]) -> list[str]:
    scored_factors = sorted(_historical_aggregate_factors(factors), key=_factor_score_impact, reverse=True)
    return [item.name for item in scored_factors if _factor_score_impact(item) > 0 and item.score >= 52][:4]


def top_negative_factors(factors: list[StandardFactor]) -> list[str]:
    historical_factors = _historical_aggregate_factors(factors)
    return [item.name for item in sorted(historical_factors, key=_factor_score_impact) if _factor_score_impact(item) < 0 and item.score <= 55][:4]


def factor_lab_notes(
    feature: FeatureSnapshot,
    profile_label: str,
    calibration_sample_count: int,
    *,
    scoring_factor_count: int | None = None,
    calibration_factor_count: int | None = None,
    uncalibrated_factor_names: tuple[str, ...] = (),
) -> list[str]:
    participation_notes: list[str] = []
    if scoring_factor_count is not None and calibration_factor_count is not None:
        if uncalibrated_factor_names:
            names = "、".join(uncalibrated_factor_names)
            participation_notes.append(
                f"当前 {scoring_factor_count} 个因子参与评分，其中 {calibration_factor_count} 个参与历史校准；"
                f"未校准项：{names}，仍参与当前评分，但不纳入综合证据充分度、正负证据和历史样本聚合。"
            )
        else:
            participation_notes.append(
                f"当前 {scoring_factor_count} 个因子参与评分，均参与历史校准。"
            )
    calibration_scope = (
        f"{calibration_factor_count} 个参与历史校准因子"
        if calibration_factor_count is not None
        else "参与因子"
    )
    return [
        "因子实验室只校验单只股票自身的历史相似状态，不做组合选股或自动交易。",
        "历史校准使用日K向后5日/10日表现，样本少时只作为证据充分度较低的参考。",
        f"当前画像为「{profile_label}」，因子权重已按画像动态调整。",
        *participation_notes,
        (
            f"汇总有效样本按{calibration_scope}的最低单因子相似样本数计为 {calibration_sample_count} 个，"
            "不跨因子累加可能重复的交易日。"
        ),
        *([f"数据质量为{feature.data_quality_level}，所有因子已按低证据充分度口径解释。"] if feature.data_quality_score < 70 else []),
    ]


def assemble_factor_lab_report(
    feature: FeatureSnapshot,
    profile_label: str,
    weight_policy: list[str],
    factors: list[StandardFactor],
) -> FactorLabReport:
    metrics = build_factor_lab_metrics(factors, feature)
    return FactorLabReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        total_score=metrics.total_score,
        calibrated_confidence=metrics.calibrated_confidence,
        evidence_sufficiency=metrics.calibrated_confidence,
        composite_reliability_level=_evidence_sufficiency_level(metrics.calibrated_confidence),
        calibration_sample_count=metrics.calibration_sample_count,
        positive_factor_count=len(metrics.positives),
        negative_factor_count=len(metrics.negatives),
        profile_label=profile_label,
        weight_policy=weight_policy,
        factors=factors,
        top_positive=metrics.positives,
        top_negative=metrics.negatives,
        summary=_factor_lab_summary(metrics.total_score, metrics.calibrated_confidence, metrics.positives, metrics.negatives),
        notes=factor_lab_notes(
            feature,
            profile_label,
            metrics.calibration_sample_count,
            scoring_factor_count=metrics.scoring_factor_count,
            calibration_factor_count=metrics.calibration_factor_count,
            uncalibrated_factor_names=metrics.uncalibrated_factor_names,
        ),
    )


def _evidence_sufficiency_level(score: int) -> str:
    if score >= 75:
        return "较高"
    if score >= 55:
        return "中等"
    if score >= 35:
        return "较低"
    return "不足"


__all__ = [
    "FactorLabMetrics",
    "_effective_calibration_sample_count",
    "_factor_participates_in_historical_calibration",
    "assemble_factor_lab_report",
    "build_factor_lab_metrics",
    "factor_lab_notes",
    "factor_risk_count",
    "factor_support_count",
    "top_negative_factors",
    "top_positive_factors",
]
