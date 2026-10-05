from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass

from app.models.research import (
    FactorLabReport,
    FactorEvidenceSupport,
    FactorScoreAggregation,
    StandardFactor,
)
from app.models.analysis import (
    FeatureSnapshot,
)
from app.services.research_factor_scoring import (
    MIN_FACTOR_CONFIRMATION_SAMPLES,
    _factor_participates_in_historical_aggregate,
    _historical_aggregate_factors,
    _weighted_factor_score,
)
from app.services.research_factor_text import _factor_lab_summary, _factor_score_impact
from app.services.research_factor_aggregation import aggregate_factor_scores
from app.services.research_execution_model import MODELLED_ROUND_TRIP_FRICTION_PCT
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float


FULL_EVIDENCE_SAMPLE_THRESHOLD = 30
EVIDENCE_SUFFICIENCY_VERSION = "factor-evidence-sufficiency.v2"
EVIDENCE_SUFFICIENCY_NOTE = (
    "证据充分度取数据质量、历史校准覆盖和样本支持度的最小值；方向分、胜率与盈亏不加分。"
    "样本支持按固定因子份额平均，缺失为0，每项30个样本达到工程上限；不代表独立样本或预测概率。"
)


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
    unavailable_factor_names: tuple[str, ...] = ()
    evidence_support: FactorEvidenceSupport | None = None


def build_factor_lab_metrics(factors: list[StandardFactor], feature: FeatureSnapshot) -> FactorLabMetrics:
    scoring_factors = [item for item in factors if item.participates_in_current_score]
    total_score = _weighted_factor_score(factors)
    historical_factors = _historical_aggregate_factors(factors)
    calibration_sample_count = _effective_calibration_sample_count(historical_factors)
    calibration_factor_count = len(historical_factors)
    uncalibrated_factor_names = tuple(
        item.name for item in scoring_factors if not _factor_participates_in_historical_calibration(item)
    )
    unavailable_factor_names = tuple(item.name for item in factors if not item.participates_in_current_score and item.aggregation_role != "composite")
    support = _factor_evidence_support(factors, feature)
    calibrated_confidence = _clamp(round(min(
        support.data_quality_score, support.calibration_coverage_pct, support.sample_support_pct,
    )))
    return FactorLabMetrics(
        total_score=total_score,
        calibrated_confidence=calibrated_confidence,
        calibration_sample_count=calibration_sample_count,
        positives=top_positive_factors(historical_factors),
        negatives=top_negative_factors(historical_factors),
        scoring_factor_count=len(scoring_factors),
        calibration_factor_count=calibration_factor_count,
        uncalibrated_factor_names=uncalibrated_factor_names,
        unavailable_factor_names=unavailable_factor_names,
        evidence_support=support,
    )


def _factor_evidence_support(factors: list[StandardFactor], feature: FeatureSnapshot) -> FactorEvidenceSupport:
    required = [item for item in factors if item.aggregation_role == "independent"
                and (finite_float(item.weight) or 0) > 0]
    calibrated = _historical_aggregate_factors(required)
    samples = _effective_calibration_sample_count(calibrated)
    supported = sum(min(1, item.calibration.sample_count / FULL_EVIDENCE_SAMPLE_THRESHOLD)
                    for item in calibrated if item.calibration is not None)
    return FactorEvidenceSupport(
        data_quality_score=_clamp(feature.data_quality_score),
        calibration_coverage_pct=100 * len(calibrated) / len(required) if required else 0,
        sample_support_pct=100 * supported / len(required) if required else 0,
        required_factor_count=len(required), calibrated_factor_count=len(calibrated),
        minimum_similar_samples=samples, full_support_sample_threshold=FULL_EVIDENCE_SAMPLE_THRESHOLD,
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
        and item.calibration.sample_count >= MIN_FACTOR_CONFIRMATION_SAMPLES
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
    unavailable_factor_names: tuple[str, ...] = (),
) -> list[str]:
    participation_notes: list[str] = []
    if scoring_factor_count is not None and calibration_factor_count is not None:
        if uncalibrated_factor_names:
            names = "、".join(uncalibrated_factor_names)
            participation_notes.append(
                f"当前 {scoring_factor_count} 个因子参与评分，其中 {calibration_factor_count} 个参与历史校准；"
                f"未校准项：{names}，仍参与当前评分，但不提供历史支持、不纳入正负证据和历史样本聚合；"
                "独立因子的固定份额保留，缺少历史支持会降低覆盖与证据充分度。"
            )
        else:
            participation_notes.append(
                f"当前 {scoring_factor_count} 个因子参与评分，均参与历史校准。"
            )
        if unavailable_factor_names:
            participation_notes.append(
                f"当前证据不可用且已从评分剔除：{'、'.join(unavailable_factor_names)}。"
            )
    calibration_scope = (
        f"{calibration_factor_count} 个参与历史校准因子"
        if calibration_factor_count is not None
        else "参与因子"
    )
    return [
        "因子实验室只校验单只股票自身的历史相似状态，不做组合选股或自动交易。",
        "龙头强度是趋势、量价及行业的复合观察，保留解释但不重复计入综合分。",
        (
            "历史校准按信号后下一交易日开盘模拟入场，5日/10日收益扣除"
            f" {MODELLED_ROUND_TRIP_FRICTION_PCT:.2f}% 标准化往返摩擦，并按10日窗口去重。"
        ),
        f"当前画像为「{profile_label}」，仅作说明；方向组和组内份额固定，不因当日换手或量能切换。",
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
    aggregation = aggregate_factor_scores(factors)
    factors = _factors_with_score_shares(factors, aggregation.factor_shares)
    metrics = build_factor_lab_metrics(factors, feature)
    return FactorLabReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        total_score=metrics.total_score,
        calibrated_confidence=metrics.calibrated_confidence,
        evidence_sufficiency=metrics.calibrated_confidence,
        evidence_sufficiency_version=EVIDENCE_SUFFICIENCY_VERSION,
        evidence_sufficiency_note=EVIDENCE_SUFFICIENCY_NOTE,
        evidence_support=metrics.evidence_support,
        score_aggregation=FactorScoreAggregation.model_validate(asdict(aggregation)),
        composite_reliability_level=_evidence_sufficiency_level(metrics.calibrated_confidence),
        calibration_sample_count=metrics.calibration_sample_count,
        positive_factor_count=len(metrics.positives),
        negative_factor_count=len(metrics.negatives),
        profile_label=profile_label,
        weight_policy=[
            "趋势与价位、量价、估值三组各占1/3；组内份额固定，缺失回到中性，不转给其它因子。",
            "风险压力仅扣分；数据质量影响证据充分度，不直接增加方向分。预算和扣分系数未经收益概率校准。",
        ],
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
            unavailable_factor_names=metrics.unavailable_factor_names,
        ),
    )


def _factors_with_score_shares(factors: list[StandardFactor], shares: dict[str, float]) -> list[StandardFactor]:
    output = []
    for factor in factors:
        share = shares.get(factor.id, 0.0)
        if factor.aggregation_role == "composite":
            usage = "observation"
        elif factor.id == "risk_pressure":
            usage = "risk_constraint"
        else:
            usage = "direction" if share > 0 else "excluded"
        output.append(factor.model_copy(update={
            "weight": share / 100, "score_share_pct": share, "score_usage": usage,
            "participates_in_current_score": factor.participates_in_current_score and usage != "excluded",
        }))
    return output


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
