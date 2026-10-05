"""Shared current/replay price-volume rule; admission belongs to each caller."""

from __future__ import annotations

from collections.abc import Sequence

from app.models.market import Kline
from app.services.scoring import clamp_score
from app.utils.market_data import finite_float, valid_kline, valid_positive_number


PRICE_VOLUME_SCORE_RULE_VERSION = "current-price-volume.v2"
PRICE_VOLUME_BASELINE_DAYS = 5
# Engineering bounds, not calibrated return probabilities. Volume adjusts
# the strength of observed price direction and never creates direction.
PRICE_VOLUME_NEUTRAL_SCORE = 50
PRICE_VOLUME_RETURN_SCALE = 5.0
PRICE_VOLUME_RETURN_CAP = 40.0
PRICE_VOLUME_MULTIPLIER_MIN = 0.5
PRICE_VOLUME_MULTIPLIER_MAX = 1.25


def price_volume_change_pct(price: object, previous_close: object) -> float | None:
    current, previous = finite_float(price), finite_float(previous_close)
    if current is None or previous is None or current <= 0 or previous <= 0:
        return None
    return finite_float((current - previous) / previous * 100)


def completed_price_volume_ratio(current_volume: object, completed_rows: Sequence[Kline]) -> float | None:
    """Use exactly the last five supplied completed bars; never filter/refill."""
    current = finite_float(current_volume)
    baseline = completed_rows[-PRICE_VOLUME_BASELINE_DAYS:]
    if current is None or current <= 0 or len(baseline) != PRICE_VOLUME_BASELINE_DAYS:
        return None
    if any(not valid_kline(row) or not valid_positive_number(row.volume) for row in baseline):
        return None
    # Divide before summing so finite large volumes cannot overflow the mean.
    average = finite_float(sum(row.volume / PRICE_VOLUME_BASELINE_DAYS for row in baseline))
    if average is None or average <= 0:
        return None
    ratio = finite_float(current / average)
    return ratio if ratio is not None and ratio > 0 else None


def price_volume_score(change_pct: object, volume_ratio: object) -> int:
    """Score admitted inputs with the same versioned rule in current and replay."""
    change, ratio = finite_float(change_pct), finite_float(volume_ratio)
    if change is None or ratio is None or ratio <= 0:
        raise ValueError("量价方向评分缺少有效涨跌幅或正量比")
    direction = max(-PRICE_VOLUME_RETURN_CAP, min(PRICE_VOLUME_RETURN_CAP, change * PRICE_VOLUME_RETURN_SCALE))
    multiplier = max(PRICE_VOLUME_MULTIPLIER_MIN, min(PRICE_VOLUME_MULTIPLIER_MAX, ratio))
    return clamp_score(PRICE_VOLUME_NEUTRAL_SCORE + direction * multiplier, round_value=True)


__all__ = [
    "PRICE_VOLUME_BASELINE_DAYS",
    "PRICE_VOLUME_SCORE_RULE_VERSION",
    "completed_price_volume_ratio",
    "price_volume_change_pct",
    "price_volume_score",
]
