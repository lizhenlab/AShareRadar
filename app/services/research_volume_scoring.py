"""Versioned 5/20-session volume confirmation for current and replay factors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.models.market import Kline
from app.services.indicator_volume import recent_volume_ratio_if_available
from app.services.price_volume_scoring import price_volume_change_pct, price_volume_score
from app.services.trading_calendar import TradingCalendarCoverageError, trading_dates_between
from app.utils.market_data import valid_kline, valid_positive_number


VOLUME_CONFIRMATION_SCORE_RULE_VERSION = "factor-volume-confirmation.v3"
VOLUME_CONFIRMATION_WINDOW_SESSIONS = 20


def volume_confirmation_inputs(rows: Sequence[Kline]) -> tuple[float, float] | None:
    """Admit one exact completed window with a comparable price and volume basis.

    A daily adjustment factor does not identify its normalization direction or
    a common anchor. Do not infer either across corporate actions. PIT snapshot
    provenance remains an additional requirement for historical calibration.
    """
    if len(rows) != VOLUME_CONFIRMATION_WINDOW_SESSIONS or not _volume_window_is_comparable(rows):
        return None
    change = price_volume_change_pct(rows[-1].close, rows[-2].close)
    ratio = recent_volume_ratio_if_available(list(rows))
    return (change, ratio) if change is not None and ratio is not None and ratio > 0 else None


def _volume_window_is_comparable(rows: Sequence[Kline]) -> bool:
    if any(
        not valid_kline(row) or not valid_positive_number(row.volume)
        or row.adjustment_mode != "qfq" or row.session_status != "trading"
        or row.corporate_action_status != "none"
        for row in rows
    ):
        return False
    try:
        dates = tuple(date.fromisoformat(row.date) for row in rows)
        if any(day.isoformat() != row.date for day, row in zip(dates, rows, strict=True)):
            return False
        return dates == trading_dates_between(dates[0], dates[-1])
    except (ValueError, TradingCalendarCoverageError):
        return False


def volume_confirmation_score(change_pct: float, volume_ratio: float) -> int:
    """Apply a bounded directional rule to an admitted 5/20-session volume ratio.

    Before integer rounding: 50 + clip(5 * change_pct, -40, 40)
    * clip(volume_ratio, 0.5, 1.25). Volume only changes the strength of the
    observed price direction. This is a price-volume proxy, not money flow.
    Current and replay callers share the exact-window admission above.
    """
    return price_volume_score(change_pct, volume_ratio)


__all__ = [
    "VOLUME_CONFIRMATION_SCORE_RULE_VERSION", "VOLUME_CONFIRMATION_WINDOW_SESSIONS",
    "volume_confirmation_inputs", "volume_confirmation_score",
]
