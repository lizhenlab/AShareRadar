from __future__ import annotations

from app.models.schemas import AnalysisResult, SignalValidationReport, TimeframeAlignmentReport
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float


def _positive_or_zero(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 0


def _non_negative_or_zero(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _positive_or_one(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 1


def _score_or_zero(value: object) -> int:
    return _clamp(value)


def _upside_level_available(value: object | None, price: float) -> bool:
    return _upside_level_or_zero(value, price) > 0


def _downside_level_available(value: object | None, price: float) -> bool:
    return _downside_level_or_zero(value, price) > 0


def _upside_level_or_zero(value: object | None, price: float) -> float:
    level = _positive_or_zero(value)
    return level if level > 0 and (price <= 0 or level > price) else 0


def _downside_level_or_zero(value: object | None, price: float) -> float:
    level = _positive_or_zero(value)
    return level if level > 0 and (price <= 0 or level < price) else 0


def _validation_status_text(validation: SignalValidationReport) -> str:
    return _text_or_default(getattr(validation, "overall_status", None), "等待确认")


def _analysis_action_text(analysis: AnalysisResult) -> str:
    action_advice = getattr(analysis, "action_advice", None)
    return _text_or_default(getattr(action_advice, "action", None), "观察")


def _timeframe_conflict_text(timeframe: TimeframeAlignmentReport | None) -> str:
    return _text_or_default(getattr(timeframe, "conflict_level", None), "") if timeframe else ""


def _text_or_default(value: object, default: str) -> str:
    if _is_non_finite_text_value(value):
        return default
    text = str(value or "").strip()
    return text or default


def _is_non_finite_text_value(value: object) -> bool:
    text = str(value).strip().lower()
    return text in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
