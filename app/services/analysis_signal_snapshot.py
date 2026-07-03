from __future__ import annotations

from app.models.schemas import DataQuality, SignalContribution, SignalSnapshot
from app.services.scoring import clamp_score
from app.utils.market_data import finite_float


MAX_SIGNAL_ITEMS = 5
DEFAULT_SIGNAL_LABEL = "待确认"
INVALID_TEXT_VALUES = {"", "none", "null", "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


def signal_snapshot(
    score: int,
    label: str,
    contributions: list[SignalContribution],
    quality: DataQuality,
    risk_level: str,
) -> SignalSnapshot:
    clean_score = _score_value(score)
    clean_label = _text_or_default(label, DEFAULT_SIGNAL_LABEL)
    clean_quality = _quality_view(quality)
    clean_contributions = _clean_contributions(contributions)
    positive = sorted([item for item in clean_contributions if item.impact > 0], key=lambda item: item.impact, reverse=True)
    negative = sorted([item for item in clean_contributions if item.impact < 0], key=lambda item: item.impact)
    neutral = [item for item in clean_contributions if item.impact == 0]
    confidence = signal_confidence(clean_score, quality)
    risk_notes = _risk_notes(risk_level, clean_quality, negative)
    return SignalSnapshot(
        score=clean_score,
        label=clean_label,
        confidence=confidence,
        summary=signal_summary(clean_score, clean_label, positive, negative, quality),
        contributions=clean_contributions,
        positive=positive[:MAX_SIGNAL_ITEMS],
        negative=negative[:MAX_SIGNAL_ITEMS],
        neutral=neutral[:MAX_SIGNAL_ITEMS],
        data_quality_notes=_clean_text_items(getattr(quality, "notes", []), MAX_SIGNAL_ITEMS),
        risk_notes=risk_notes,
    )


def signal_confidence(score: int, quality: DataQuality) -> int:
    clean_score = _score_value(score)
    quality_score = _quality_view(quality).score
    signal_clarity = min(100, 50 + abs(clean_score - 50))
    confidence = round(quality_score * 0.65 + signal_clarity * 0.35)
    return max(20, min(95, confidence))


def signal_summary(
    score: int,
    label: str,
    positive: list[SignalContribution],
    negative: list[SignalContribution],
    quality: DataQuality,
) -> str:
    clean_positive = [item for item in _clean_contributions(positive) if item.impact > 0]
    clean_negative = [item for item in _clean_contributions(negative) if item.impact < 0]
    drivers = []
    if clean_positive:
        drivers.append(f"主要加分来自{clean_positive[0].name}")
    if clean_negative:
        drivers.append(f"主要扣分来自{clean_negative[0].name}")
    driver_text = "，".join(drivers) if drivers else "暂无明显单项驱动"
    clean_quality = _quality_view(quality)
    return f"趋势评分 {_score_value(score)}/100，状态为{_text_or_default(label, DEFAULT_SIGNAL_LABEL)}；{driver_text}；数据质量{clean_quality.level}{clean_quality.score}分。"


def _risk_notes(risk_level: str, quality: "_QualityView", negative: list[SignalContribution]) -> list[str]:
    notes = []
    clean_risk_level = _clean_text(risk_level)
    if clean_risk_level in {"中等风险", "高风险"}:
        notes.append(f"当前风险级别为{clean_risk_level}。")
    if quality.score < 70:
        notes.append(f"数据质量{quality.level}，结论已自动降权。")
    notes.extend(item.reason for item in negative[:2])
    return _dedupe_texts(notes)[:MAX_SIGNAL_ITEMS]


def _clean_contributions(contributions: list[SignalContribution]) -> list[SignalContribution]:
    result: list[SignalContribution] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for item in contributions or []:
        cleaned = _clean_contribution(item)
        if cleaned is None:
            continue
        key = (cleaned.category, cleaned.name, cleaned.impact, cleaned.level, cleaned.reason)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _clean_contribution(item: SignalContribution) -> SignalContribution | None:
    impact = _impact_value(getattr(item, "impact", None))
    name = _clean_text(getattr(item, "name", None))
    reason = _clean_text(getattr(item, "reason", None))
    if impact is None or not name or not reason:
        return None
    return SignalContribution(
        category=_text_or_default(getattr(item, "category", None), "未分类"),
        name=name,
        impact=impact,
        level=_text_or_default(getattr(item, "level", None), "观察"),
        reason=reason,
    )


def _impact_value(value: object) -> int | None:
    parsed = finite_float(value)
    return round(parsed) if parsed is not None else None


def _score_value(value: object) -> int:
    return clamp_score(value, round_value=True)


class _QualityView:
    def __init__(self, score: int, level: str) -> None:
        self.score = score
        self.level = level


def _quality_view(quality: DataQuality) -> _QualityView:
    return _QualityView(
        score=clamp_score(getattr(quality, "score", None), default=0, round_value=True),
        level=_text_or_default(getattr(quality, "level", None), "待确认"),
    )


def _clean_text_items(items: object, limit: int) -> list[str]:
    if isinstance(items, str):
        candidates = [items]
    else:
        try:
            candidates = list(items or [])
        except TypeError:
            candidates = [items]
    return _dedupe_texts(candidates)[:limit]


def _dedupe_texts(items) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _clean_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _text_or_default(value: object, default: str) -> str:
    return _clean_text(value) or default


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in INVALID_TEXT_VALUES else text


__all__ = ["signal_snapshot", "signal_confidence", "signal_summary"]
