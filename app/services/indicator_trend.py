from __future__ import annotations

from app.models.schemas import Kline, Quote, SignalContribution
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


def trend_score(quote: Quote, klines: list[Kline]) -> tuple[int, str]:
    score, label, _ = trend_score_snapshot(quote, klines)
    return score, label


def trend_score_snapshot(quote: Quote, klines: list[Kline]) -> tuple[int, str, list[SignalContribution]]:
    valid_klines = filter_valid_klines(klines)
    if len(valid_klines) < 20:
        return 50, "数据不足", insufficient_sample_contributions()
    contributions = trend_contributions(build_trend_context(quote, valid_klines))
    score = 50 + sum(item.impact for item in contributions)
    score = max(0, min(100, score))
    return score, _trend_label(score), contributions


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
    "trend_score_snapshot",
]
