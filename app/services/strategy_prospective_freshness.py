"""Offline source recency diagnostics, separate from immutable input admission."""

from __future__ import annotations

from datetime import date, datetime

from app.services.trading_calendar import (
    DAILY_KLINE_PUBLISH_TIME, TradingCalendarCoverageError, latest_expected_daily_kline_date,
)
from app.utils.clock import ASHARE_TIMEZONE


def prospective_source_freshness(source_date: str | None, now: datetime) -> dict[str, object]:
    """Compare source identity to the last session whose daily data should exist.

    Missing calendar coverage cannot be substituted with a weekday assumption.
    This observation neither changes the frozen signal axis nor admits capture.
    """
    result: dict[str, object] = {
        "status": "unknown", "source_date": source_date, "expected_published_trade_date": None,
        "basis": "latest_expected_daily_kline_date", "calendar_policy": "local_only_no_auto_refresh",
        "publication_cutoff_local": DAILY_KLINE_PUBLISH_TIME.isoformat(), "timezone": "Asia/Shanghai",
        "calendar_coverage": "unknown", "reason": "trusted_calendar_coverage_required",
    }
    try:
        expected = latest_expected_daily_kline_date(now, allow_auto_refresh=False)
    except TradingCalendarCoverageError:
        return result
    result.update(expected_published_trade_date=expected.isoformat(), calendar_coverage="covered")
    if source_date is None:
        return {**result, "status": "no_source", "reason": "published_source_required"}
    source = date.fromisoformat(source_date)
    if source.isoformat() != source_date:
        raise ValueError("prospective source date is not canonical")
    status = "current" if source == expected else "stale" if source < expected else "ahead_of_expected"
    reasons = {"current": None, "stale": "source_before_expected_published_session",
               "ahead_of_expected": "source_after_expected_published_session"}
    return {**result, "status": status, "reason": reasons[status],
            "calendar_days_since_source": (now.astimezone(ASHARE_TIMEZONE).date() - source).days}
