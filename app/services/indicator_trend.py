from __future__ import annotations

import math

from app.models.market import (
    Kline,
    Quote,
)
from app.models.analysis import (
    SignalContribution,
)
from app.models.market_scan import MarketScanMode
from app.services.indicator_trend_components import (
    build_trend_context,
    change_impact as _change_impact,
    contribution as _contribution,
    impact_level as _impact_level,
    insufficient_sample_contributions,
    trend_contributions,
    trend_label as _trend_label,
    turnover_signal as _turnover_signal,
    volume_signal as _volume_signal,
)
from app.utils.market_data import filter_valid_klines


TREND_SCORE_CENTER = 50
TREND_SCORE_SOFT_CLIP_SCALE = 50.0


def trend_score(
    quote: Quote,
    klines: list[Kline],
    *,
    mode: MarketScanMode = "official",
) -> tuple[int, str]:
    score, label, _ = trend_score_snapshot(quote, klines, mode=mode)
    return score, label


def trend_score_snapshot(
    quote: Quote,
    klines: list[Kline],
    *,
    mode: MarketScanMode = "official",
) -> tuple[int, str, list[SignalContribution]]:
    valid_klines = filter_valid_klines(klines)
    if len(valid_klines) < 20:
        return 50, "数据不足", insufficient_sample_contributions()
    contributions = trend_contributions(build_trend_context(quote, valid_klines, mode=mode))
    score = trend_score_from_impact(sum(item.impact for item in contributions))
    return score, _trend_label(score), contributions


def trend_score_from_impact(total_impact: int | float) -> int:
    if not math.isfinite(total_impact):
        return TREND_SCORE_CENTER
    score = TREND_SCORE_CENTER + TREND_SCORE_CENTER * math.tanh(total_impact / TREND_SCORE_SOFT_CLIP_SCALE)
    return max(0, min(100, round(score)))


def _add_contribution(
    contributions: list[SignalContribution],
    category: str,
    name: str,
    impact: int,
    reason: str,
) -> int:
    contributions.append(_contribution(category, name, impact, reason))
    return impact


__all__ = [
    "_add_contribution",
    "_change_impact",
    "_impact_level",
    "_trend_label",
    "_turnover_signal",
    "_volume_signal",
    "trend_score",
    "trend_score_from_impact",
    "trend_score_snapshot",
]
