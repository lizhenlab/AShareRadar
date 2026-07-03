from __future__ import annotations

from app.models.schemas import FactorCalibration, FactorLabReport, StandardFactor
from app.services.research_factor_scoring import _dedupe


def _factor_score_impact(factor: StandardFactor) -> int:
    base = round((factor.score - 50) / 2)
    calibration = factor.calibration
    if not calibration or calibration.sample_count <= 0:
        return base
    if calibration.expected_level in {"较强", "偏正"}:
        return base + max(0, min(4, round((calibration.stability_score - 50) / 18)))
    if calibration.expected_level in {"偏弱", "风险"}:
        return base - max(0, min(4, round((50 - calibration.stability_score) / 16)))
    return base + max(0, min(2, round((calibration.stability_score - 50) / 28)))


def _factor_calibration_impact(calibration: FactorCalibration) -> int:
    if calibration.sample_count < 5:
        return 0
    if calibration.expected_level in {"较强", "偏正"}:
        return max(0, min(4, round((calibration.stability_score - 50) / 16)))
    if calibration.expected_level in {"偏弱", "风险"}:
        return -max(0, min(4, round((50 - calibration.stability_score) / 14)))
    return max(0, min(2, round((calibration.stability_score - 50) / 24)))


def _factor_lab_summary(total_score: int, confidence: int, positives: list[str], negatives: list[str]) -> str:
    positive_text = "、".join(positives) if positives else "暂无明确正向因子"
    negative_text = "、".join(negatives) if negatives else "暂无核心拖累因子"
    if total_score >= 65 and confidence >= 60:
        tone = "因子结构偏积极"
    elif total_score <= 48:
        tone = "因子结构偏谨慎"
    else:
        tone = "因子结构仍需确认"
    return f"{tone}：主要支撑来自{positive_text}；主要拖累来自{negative_text}。"


def _factor_alpha_reason(factor: StandardFactor) -> str:
    calibration = factor.calibration
    if calibration and calibration.sample_count > 0:
        bucket_text = _factor_bucket_alpha_text(factor)
        if calibration.sample_count < 5:
            return (
                f"{factor.value}；历史相似样本仅 {calibration.sample_count} 次，"
                f"样本偏少，暂不把胜率用于提高结论权重。{bucket_text}"
            )
        return (
            f"{factor.value}；历史相似样本 {calibration.sample_count} 次，"
            f"5日胜率 {calibration.win_rate:.1f}%，5日均值 {calibration.avg_forward_5d_return:.2f}%，"
            f"稳定性 {calibration.confidence_level}/{calibration.expected_level}。{bucket_text}"
        )
    return f"{factor.value}；历史校准为「{calibration.confidence_level if calibration else '暂无'}」。"


def _factor_missing_data(factor_lab: FactorLabReport) -> list[str]:
    return _dedupe([item for factor in factor_lab.factors for item in factor.missing_data])


def _factor_bucket_alpha_text(factor: StandardFactor) -> str:
    if not factor.calibration_buckets:
        return ""
    best = sorted(factor.calibration_buckets, key=lambda item: (item.sample_count >= 5, item.avg_forward_5d_return), reverse=True)[0]
    return f"分层校准中「{best.name}」样本 {best.sample_count} 个，5日均值 {best.avg_forward_5d_return:.2f}%。"


def _find_factor(factor_lab: FactorLabReport, factor_id: str) -> StandardFactor | None:
    return next((item for item in factor_lab.factors if item.id == factor_id), None)


def _factor_reference(factor: StandardFactor | None) -> str:
    if not factor or not factor.calibration:
        return "暂无可用历史参考。"
    calibration = factor.calibration
    if calibration.sample_count <= 0:
        return calibration.note
    return (
        f"历史相似样本 {calibration.sample_count} 个，5日胜率 {calibration.win_rate:.1f}%，"
        f"平均5日 {calibration.avg_forward_5d_return:.2f}%，最大不利 {calibration.max_adverse_return:.2f}%。"
    )


def _factor_confirmation_text(factor_lab: FactorLabReport) -> str:
    main = factor_lab.top_positive[0] if factor_lab.top_positive else "正向因子"
    if factor_lab.calibration_sample_count >= 20:
        return f"因子实验室由「{main}」提供支撑，校准样本 {factor_lab.calibration_sample_count} 个，置信度 {factor_lab.calibrated_confidence}%。"
    return f"因子实验室出现「{main}」支撑，但样本只有 {factor_lab.calibration_sample_count} 个，仍需价量确认。"


def _factor_risk_text(factor_lab: FactorLabReport) -> str:
    main = factor_lab.top_negative[0] if factor_lab.top_negative else "负向因子"
    if factor_lab.negative_factor_count >= factor_lab.positive_factor_count:
        return f"因子实验室负向因子不少，尤其要看「{main}」是否修复。"
    return f"虽然整体不完全悲观，但「{main}」仍是当前拖累项。"


__all__ = [
    "_factor_alpha_reason",
    "_factor_bucket_alpha_text",
    "_factor_calibration_impact",
    "_factor_confirmation_text",
    "_factor_lab_summary",
    "_factor_missing_data",
    "_factor_reference",
    "_factor_risk_text",
    "_factor_score_impact",
    "_find_factor",
]
