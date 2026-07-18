from __future__ import annotations

from app.models.schemas import AnalysisResult, ChipAnalysis, FactorLabReport, FeatureSnapshot, LeadershipReport, StockInsightBundle
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_report import assemble_factor_lab_report
from app.services.research_factor_scoring import (
    _build_factor,
    _chip_position_evidence,
    _chip_position_score_current,
    _chip_position_value,
    _dedupe,
    _factor_calibration_quality,
    _factor_direction,
    _risk_pressure_score,
    _volume_confirmation_score,
    _weighted_factor_score,
)
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_text import (
    _factor_alpha_reason,
    _factor_bucket_alpha_text,
    _factor_calibration_impact,
    _factor_confirmation_text,
    _factor_evidence_sufficiency,
    _factor_lab_summary,
    _factor_missing_data,
    _factor_reference,
    _factor_risk_text,
    _factor_score_impact,
    _find_factor,
)
from app.services.research_factor_weights import _adjusted_factor_weight, _factor_weight_policy


def build_factor_lab_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    chip: ChipAnalysis | None = None,
    leadership: LeadershipReport | None = None,
) -> FactorLabReport:
    profile_label, weight_adjustments, weight_policy = _factor_weight_policy(analysis, feature)
    factors = build_current_factors(
        analysis,
        insights,
        feature,
        chip,
        leadership,
        weight_adjustments,
    )
    return assemble_factor_lab_report(feature, profile_label, weight_policy, factors)


__all__ = [
    "_adjusted_factor_weight",
    "_build_factor",
    "_chip_position_evidence",
    "_chip_position_score_current",
    "_chip_position_value",
    "_dedupe",
    "_factor_alpha_reason",
    "_factor_bucket_alpha_text",
    "_factor_calibration_impact",
    "_factor_calibration_quality",
    "_factor_confirmation_text",
    "_factor_evidence_sufficiency",
    "_factor_direction",
    "_factor_lab_summary",
    "_factor_missing_data",
    "_factor_reference",
    "_factor_risk_text",
    "_factor_score_impact",
    "_factor_specs",
    "_factor_weight_policy",
    "_find_factor",
    "_risk_pressure_score",
    "_volume_confirmation_score",
    "_weighted_factor_score",
    "build_current_factors",
    "build_factor_lab_report",
]
